[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Hub', 'Employee')]
    [string]$Role,
    [string]$Repo = '31-hub-st/Automated-video-editing-tools',
    [string]$InstallRoot = '',
    [string]$DataRoot = '',
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [string]$TaskName = 'StoryForge Hub',
    [switch]$RestoreHubData,
    [switch]$ReplaceExistingData,
    [switch]$FreshReplacementHost
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
        throw "$Label must be ASCII-only, for example D:\StoryForgeHub."
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

function Assert-ExistingOrdinaryDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label exists but is not a directory: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label cannot be a reparse point: $Path"
    }
}

function Get-FreshReplacementDataState {
    param([Parameter(Mandatory = $true)][string]$DataRoot)

    if (-not (Test-Path -LiteralPath $DataRoot)) {
        return @()
    }
    Assert-ExistingOrdinaryDirectory -Path $DataRoot -Label 'Fresh replacement DataRoot'
    $fullRoot = [System.IO.Path]::GetFullPath($DataRoot).TrimEnd('\')
    $items = @(
        Get-ChildItem -LiteralPath $fullRoot -Recurse -Force -ErrorAction Stop
    )
    $records = @(
        foreach ($item in $items) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Fresh replacement DataRoot contains a reparse point: $($item.FullName)"
            }
            $relative = $item.FullName.Substring($fullRoot.Length).TrimStart('\').Replace('\', '/')
            if ($item.PSIsContainer) {
                [PSCustomObject]@{
                    relative = $relative
                    kind = 'directory'
                    size = [long]0
                    sha256 = ''
                }
            }
            else {
                [PSCustomObject]@{
                    relative = $relative
                    kind = 'file'
                    size = [long]$item.Length
                    sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
            }
        }
    )
    return @($records | Sort-Object -Property relative)
}

function Assert-FreshReplacementDataState {
    param(
        [Parameter(Mandatory = $true)][string]$DataRoot,
        [Parameter(Mandatory = $true)][object[]]$ExpectedDataState
    )

    $actual = @(Get-FreshReplacementDataState -DataRoot $DataRoot)
    $expected = @($ExpectedDataState)
    if ($actual.Count -ne $expected.Count) {
        throw 'Fresh replacement DataRoot changed after trusted initialization.'
    }
    for ($index = 0; $index -lt $expected.Count; $index += 1) {
        $expectedItem = $expected[$index]
        $actualItem = $actual[$index]
        if (
            -not [string]::Equals(
                [string]$actualItem.relative,
                [string]$expectedItem.relative,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            [string]$actualItem.kind -ne [string]$expectedItem.kind -or
            [long]$actualItem.size -ne [long]$expectedItem.size -or
            [string]$actualItem.sha256 -ne [string]$expectedItem.sha256
        ) {
            throw 'Fresh replacement DataRoot changed after trusted initialization.'
        }
    }
}

function Assert-FreshReplacementHostState {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$DataRoot,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][int]$Port,
        [switch]$RequireEmptyData,
        [object[]]$ExpectedDataState
    )

    Assert-ExistingOrdinaryDirectory -Path $InstallRoot -Label 'Fresh replacement InstallRoot'
    Assert-ExistingOrdinaryDirectory -Path $DataRoot -Label 'Fresh replacement DataRoot'

    $tasks = @(Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
    if ($tasks.Count -gt 0) {
        throw "Scheduled task '$TaskName' appeared during fresh replacement."
    }

    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    )
    if ($listeners.Count -gt 0) {
        $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
        throw "TCP port $Port became occupied during fresh replacement. PID=$($owners -join ',')"
    }

    $storyForgeProcesses = @(
        Get-CimInstance -ClassName Win32_Process -ErrorAction Stop |
            Where-Object {
                $name = [string]$_.Name
                $executable = [string]$_.ExecutablePath
                $commandLine = [string]$_.CommandLine
                $name -ieq 'StoryForge Studio.exe' -or
                $executable -match '(?i)\\StoryForgeHub\\App-[^\\]+\\StoryForge Studio\.exe$' -or
                (
                    $commandLine -match '(?i)StoryForge Studio\.exe' -and
                    $commandLine -match '(?i)(?:--web|--local-worker)'
                )
            }
    )
    if ($storyForgeProcesses.Count -gt 0) {
        $summary = @(
            $storyForgeProcesses |
                ForEach-Object { '{0}:{1}' -f $_.ProcessId, $_.Name }
        ) -join ', '
        throw "A formal StoryForge process appeared during fresh replacement: $summary"
    }

    $actualDataState = @(Get-FreshReplacementDataState -DataRoot $DataRoot)
    if ($RequireEmptyData -and $actualDataState.Count -gt 0) {
        throw 'Fresh replacement DataRoot is no longer empty.'
    }
    if ($PSBoundParameters.ContainsKey('ExpectedDataState')) {
        Assert-FreshReplacementDataState `
            -DataRoot $DataRoot `
            -ExpectedDataState $ExpectedDataState
    }
    return $actualDataState
}

function Get-GhPath {
    $command = Get-Command -Name 'gh' -ErrorAction SilentlyContinue
    if (-not $command) {
        throw 'GitHub CLI (gh) is required. Install it and run: gh auth login --hostname github.com'
    }
    & $command.Source auth status --hostname github.com | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'GitHub CLI is not authenticated. Run: gh auth login --hostname github.com'
    }
    return $command.Source
}

function Invoke-NativeText {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $output = & $FilePath @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    $text = ($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
    if ($exitCode -ne 0) {
        throw "Command failed ($exitCode): $FilePath $($Arguments -join ' ')`n$text"
    }
    return $text
}

function Invoke-GhJson {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $text = Invoke-NativeText -FilePath $script:GhPath -Arguments $Arguments
    try {
        return $text | ConvertFrom-Json
    }
    catch {
        throw "GitHub CLI returned invalid JSON for: gh $($Arguments -join ' ')"
    }
}

function Invoke-Gh {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & $script:GhPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "GitHub CLI command failed: gh $($Arguments -join ' ')"
    }
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-DirectoryFileManifest {
    param([Parameter(Mandatory = $true)][string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "Application directory is missing: $Root"
    }
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $items = @(Get-ChildItem -LiteralPath $fullRoot -Recurse -Force -ErrorAction Stop)
    foreach ($item in $items) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Application directory contains an unsupported reparse point: $($item.FullName)"
        }
    }
    $records = @(
        $items |
            Where-Object { -not $_.PSIsContainer } |
            ForEach-Object {
                $file = $_
                $relative = $file.FullName.Substring($fullRoot.Length).TrimStart('\').Replace('\', '/')
                [PSCustomObject]@{
                    relative = $relative
                    normalized = $relative.ToLowerInvariant()
                    size = [long]$file.Length
                    sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
            } |
            Sort-Object -Property normalized
    )
    for ($index = 1; $index -lt $records.Count; $index += 1) {
        if ([string]$records[$index - 1].normalized -eq [string]$records[$index].normalized) {
            throw "Application directory contains case-insensitive duplicate paths: $($records[$index].relative)"
        }
    }
    return $records
}

function Assert-DirectoryTreesMatch {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedRoot,
        [Parameter(Mandatory = $true)][string]$ActualRoot
    )

    $expected = @(Get-DirectoryFileManifest -Root $ExpectedRoot)
    $actual = @(Get-DirectoryFileManifest -Root $ActualRoot)
    if ($expected.Count -ne $actual.Count) {
        throw "Existing application directory does not exactly match the verified release: $ActualRoot"
    }
    for ($index = 0; $index -lt $expected.Count; $index += 1) {
        if (
            [string]$expected[$index].normalized -ne [string]$actual[$index].normalized -or
            [long]$expected[$index].size -ne [long]$actual[$index].size -or
            [string]$expected[$index].sha256 -ne [string]$actual[$index].sha256
        ) {
            throw "Existing application directory does not exactly match the verified release: $ActualRoot"
        }
    }
}

function Write-DeploymentPointer {
    param(
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)]$Value
    )

    $temporary = $Destination + '.tmp-' + [Guid]::NewGuid().ToString('N')
    try {
        [System.IO.File]::WriteAllText(
            $temporary,
            (($Value | ConvertTo-Json) + [Environment]::NewLine),
            (New-Object System.Text.UTF8Encoding($false))
        )
        Move-Item -LiteralPath $temporary -Destination $Destination -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Write-DesktopLauncher {
    param(
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Entrypoint,
        [Parameter(Mandatory = $true)]
        [ValidateSet('Hub', 'Employee')]
        [string]$Role,
        [string]$DataRoot = ''
    )

    if ($Role -eq 'Hub') {
        if (-not $DataRoot) {
            throw 'Hub desktop launcher requires DataRoot.'
        }
        $environmentCommands =
            "set `"STORYFORGE_DEPLOYMENT_ROLE=Hub`"`r`n" +
            "set `"STORYFORGE_DATA_DIR=$DataRoot`"`r`n" +
            "set `"STORYFORGE_FROZEN_HUB_DATA_ROOT=`"`r`n" +
            "set `"STORYFORGE_PORTABLE_MODE=`"`r`n"
        $startArguments = ''
    }
    else {
        if ($DataRoot) {
            throw 'Employee desktop launcher must not receive DataRoot.'
        }
        $environmentCommands =
            "set `"STORYFORGE_DEPLOYMENT_ROLE=`"`r`n" +
            "set `"STORYFORGE_DATA_DIR=`"`r`n" +
            "set `"STORYFORGE_FROZEN_HUB_DATA_ROOT=`"`r`n" +
            "set `"STORYFORGE_PORTABLE_MODE=`"`r`n"
        $startArguments = ' %*'
    }

    $startCommand = "@echo off`r`n$environmentCommands" + "start `"`" `"$Entrypoint`"$startArguments`r`n"
    [System.IO.File]::WriteAllText($Destination, $startCommand, [System.Text.Encoding]::ASCII)
}

function Assert-GitHubAsset {
    param(
        [Parameter(Mandatory = $true)]$Asset,
        [Parameter(Mandatory = $true)][string]$Path
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Downloaded release asset is missing: $Path"
    }
    $length = (Get-Item -LiteralPath $Path).Length
    if ($length -ne [long]$Asset.size) {
        throw "GitHub asset size mismatch for $($Asset.name)."
    }
    $nativeDigest = [string]$Asset.digest
    if ($nativeDigest -notmatch '^sha256:([0-9a-fA-F]{64})$') {
        throw "GitHub did not provide a SHA-256 digest for $($Asset.name)."
    }
    $expected = $Matches[1].ToLowerInvariant()
    $actual = Get-Sha256 -Path $Path
    if ($actual -ne $expected) {
        throw "GitHub asset digest mismatch for $($Asset.name)."
    }
}

function Assert-SidecarFile {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$PayloadPath,
        [Parameter(Mandatory = $true)][string]$ExpectedFilename
    )
    if ([string]$Manifest.filename -ne $ExpectedFilename) {
        throw "Sidecar filename does not match $ExpectedFilename."
    }
    $length = (Get-Item -LiteralPath $PayloadPath).Length
    if ($length -ne [long]$Manifest.size_bytes) {
        throw "Sidecar size mismatch for $ExpectedFilename."
    }
    $declared = ([string]$Manifest.sha256).ToLowerInvariant()
    if ($declared -notmatch '^[0-9a-f]{64}$') {
        throw "Sidecar SHA-256 is invalid for $ExpectedFilename."
    }
    if ((Get-Sha256 -Path $PayloadPath) -ne $declared) {
        throw "Sidecar SHA-256 mismatch for $ExpectedFilename."
    }
}

function Read-VerifiedInternalManifest {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        $manifestEntry = $null
        foreach ($entry in $archive.Entries) {
            $portable = $entry.FullName.Replace('/', '\')
            if ($portable.StartsWith('\') -or $portable -match '^[A-Za-z]:') {
                throw "Release archive contains an absolute path: $($entry.FullName)"
            }
            $candidate = [System.IO.Path]::GetFullPath((Join-Path 'C:\StoryForgeArchiveRoot' $portable))
            if (-not $candidate.StartsWith('C:\StoryForgeArchiveRoot\', [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Release archive contains a path traversal entry: $($entry.FullName)"
            }
            if ($entry.FullName -eq 'storyforge-update.json') {
                $manifestEntry = $entry
            }
        }
        if (-not $manifestEntry) {
            throw 'Release archive is missing storyforge-update.json at its root.'
        }
        $reader = New-Object System.IO.StreamReader($manifestEntry.Open(), [System.Text.Encoding]::UTF8, $true)
        try {
            return $reader.ReadToEnd() | ConvertFrom-Json
        }
        finally {
            $reader.Dispose()
        }
    }
    finally {
        $archive.Dispose()
    }
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-Win32ProcessById {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    return Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
}

function Assert-HubIsOffline {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedDataRoot,
        [Parameter(Mandatory = $true)][int]$ExpectedPort
    )

    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $ExpectedPort -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0) {
        $details = New-Object System.Collections.Generic.List[string]
        foreach ($listener in $listeners) {
            try {
                $process = Get-Win32ProcessById -ProcessId ([int]$listener.OwningProcess)
                $details.Add("PID=$($process.ProcessId); name=$($process.Name); exe=$($process.ExecutablePath)")
            }
            catch {
                $details.Add("PID=$($listener.OwningProcess); process details unavailable")
            }
        }
        throw "TCP port $ExpectedPort is already in use. Stop the existing service before deployment. $($details -join ' | ')"
    }

    try {
        $processes = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
    }
    catch {
        throw "Unable to verify that the old StoryForge Hub is offline: $($_.Exception.Message)"
    }
    $related = @(
        $processes | Where-Object {
            $commandLine = [string]$_.CommandLine
            ($commandLine -match '(?i)(?:storyforge\.main|StoryForge Studio\.exe).*?(?:^|\s)--web(?:\s|$)') -or
            $commandLine -match '(?i)Start-StoryForge-Hub\.ps1'
        }
    )
    if ($related.Count -gt 0) {
        $details = @($related | ForEach-Object { "PID=$($_.ProcessId); name=$($_.Name)" }) -join ' | '
        throw "A StoryForge Hub process is still running. Stop it before deployment or restore. $details"
    }
}

if (-not [Environment]::Is64BitOperatingSystem -or $env:OS -ne 'Windows_NT') {
    throw 'StoryForge deployment requires 64-bit Windows 10 or Windows 11.'
}
if ($Repo -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    throw 'Repo must use the owner/name form.'
}
if (-not $InstallRoot) {
    $InstallRoot = if ($Role -eq 'Hub') { 'D:\StoryForgeHub' } else { 'D:\StoryForge' }
}
$InstallRoot = Get-SafeFixedPath -Value $InstallRoot -Label 'InstallRoot'
Assert-ExistingOrdinaryDirectory -Path $InstallRoot -Label 'InstallRoot'
if ($Role -eq 'Hub') {
    if (-not (Test-Administrator)) {
        throw 'Hub deployment must run in an elevated Windows PowerShell window.'
    }
    if (-not $DataRoot) {
        $DataRoot = Join-Path $InstallRoot 'Data'
    }
    $DataRoot = Get-SafeFixedPath -Value $DataRoot -Label 'DataRoot'
    Assert-ExistingOrdinaryDirectory -Path $DataRoot -Label 'DataRoot'
    if ($DataRoot -eq $InstallRoot) {
        throw 'DataRoot must be a dedicated directory, not InstallRoot itself.'
    }
}
elseif ($RestoreHubData -or $ReplaceExistingData -or $DataRoot) {
    throw 'Employee mode installs only the program; Hub data options are not allowed.'
}
if ($ReplaceExistingData -and -not $RestoreHubData) {
    throw '-ReplaceExistingData is valid only together with -RestoreHubData.'
}
if (
    $FreshReplacementHost -and
    (
        $Role -ne 'Hub' -or
        -not $RestoreHubData -or
        -not $ReplaceExistingData
    )
) {
    throw 'FreshReplacementHost requires Hub restore with ReplaceExistingData.'
}

$dataHasContent = $false
$ruleName = "StoryForge Hub $Port (Private LAN)"
if ($Role -eq 'Hub') {
    Assert-HubIsOffline -ExpectedDataRoot $DataRoot -ExpectedPort $Port
    if (Test-Path -LiteralPath $DataRoot) {
        if (-not (Test-Path -LiteralPath $DataRoot -PathType Container)) {
            throw "DataRoot is not a directory: $DataRoot"
        }
        $dataHasContent = @(
            Get-ChildItem -LiteralPath $DataRoot -Force -ErrorAction Stop
        ).Count -gt 0
    }
    if ($dataHasContent -and -not ($RestoreHubData -and $ReplaceExistingData)) {
        throw 'DataRoot already contains files. Use a new empty path, or explicitly use -RestoreHubData -ReplaceExistingData for authoritative replacement.'
    }
    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        throw "Scheduled task '$TaskName' already exists. Refusing to overwrite existing Hub configuration."
    }
    $existingRule = @(Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)
    if ($existingRule.Count -gt 0) {
        throw "Firewall rule '$ruleName' already exists. Refusing to overwrite existing Hub configuration."
    }
    if ($FreshReplacementHost) {
        [void](Assert-FreshReplacementHostState `
            -InstallRoot $InstallRoot `
            -DataRoot $DataRoot `
            -TaskName $TaskName `
            -Port $Port `
            -RequireEmptyData)
    }
}

$script:GhPath = Get-GhPath
$repository = Invoke-GhJson -Arguments @('api', "repos/$Repo")
if (-not [bool]$repository.private) {
    throw 'The StoryForge repository must remain private before program or Hub data download.'
}

[System.IO.Directory]::CreateDirectory($InstallRoot) | Out-Null
$stageRoot = Join-Path $InstallRoot ('.bootstrap-' + [Guid]::NewGuid().ToString('N'))
[System.IO.Directory]::CreateDirectory($stageRoot) | Out-Null
$hubTaskCreated = $false
$hubFirewallRuleCreated = $false
$hubProvisioningComplete = $false
$freshInitializedDataState = @()

try {
    Write-Host 'Reading the latest stable StoryForge release...'
    $release = Invoke-GhJson -Arguments @('api', "repos/$Repo/releases/latest")
    if ([bool]$release.draft -or [bool]$release.prerelease) {
        throw 'GitHub latest release is not a stable published release.'
    }
    $programAssets = @($release.assets | Where-Object { [string]$_.name -like 'StoryForge-*-update.zip' })
    if ($programAssets.Count -ne 1) {
        throw 'The latest stable release must contain exactly one StoryForge update ZIP.'
    }
    $programAsset = $programAssets[0]
    $programName = [string]$programAsset.name
    $sidecarName = $programName + '.manifest.json'
    $sidecarAssets = @($release.assets | Where-Object { [string]$_.name -eq $sidecarName })
    if ($sidecarAssets.Count -ne 1) {
        throw "The latest release is missing the exact sidecar: $sidecarName"
    }
    $downloadRoot = Join-Path $stageRoot 'download'
    [System.IO.Directory]::CreateDirectory($downloadRoot) | Out-Null
    Invoke-Gh -Arguments @(
        'release', 'download', [string]$release.tag_name,
        '--repo', $Repo, '--dir', $downloadRoot,
        '--pattern', $programName, '--pattern', $sidecarName, '--clobber'
    )
    $programZip = Join-Path $downloadRoot $programName
    $programSidecar = Join-Path $downloadRoot $sidecarName
    Assert-GitHubAsset -Asset $programAsset -Path $programZip
    Assert-GitHubAsset -Asset $sidecarAssets[0] -Path $programSidecar
    $packageManifest = Get-Content -LiteralPath $programSidecar -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-SidecarFile -Manifest $packageManifest -PayloadPath $programZip -ExpectedFilename $programName
    $version = [string]$packageManifest.version
    if (-not $version -or $version -ne (([string]$release.tag_name) -replace '^v', '')) {
        throw 'Stable release tag and package version do not match.'
    }
    $internalManifest = Read-VerifiedInternalManifest -ArchivePath $programZip
    if ([string]$internalManifest.version -ne $version) {
        throw 'Internal update manifest version does not match the signed sidecar.'
    }
    if ([string]$internalManifest.entrypoint -ne [string]$packageManifest.entrypoint) {
        throw 'Internal and external entrypoints do not match.'
    }

    $extractRoot = Join-Path $stageRoot 'extracted'
    Write-Host "Extracting StoryForge $version..."
    Expand-Archive -LiteralPath $programZip -DestinationPath $extractRoot -Force
    $extractedManifestPath = Join-Path $extractRoot 'storyforge-update.json'
    if (-not (Test-Path -LiteralPath $extractedManifestPath -PathType Leaf)) {
        throw 'Extracted release is missing storyforge-update.json.'
    }
    $extractedManifest = Get-Content -LiteralPath $extractedManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$extractedManifest.version -ne $version -or [string]$extractedManifest.entrypoint -ne [string]$packageManifest.entrypoint) {
        throw 'Extracted release manifest changed after archive verification.'
    }
    $extractedEntrypoint = Join-Path $extractRoot ([string]$packageManifest.entrypoint)
    if (-not (Test-Path -LiteralPath $extractedEntrypoint -PathType Leaf)) {
        throw "Extracted StoryForge executable is missing: $extractedEntrypoint"
    }

    $appDirectory = Join-Path $InstallRoot ("App-$version")
    if (Test-Path -LiteralPath $appDirectory) {
        Assert-DirectoryTreesMatch -ExpectedRoot $extractRoot -ActualRoot $appDirectory
    }
    else {
        Move-Item -LiteralPath $extractRoot -Destination $appDirectory
    }
    $entrypoint = Join-Path $appDirectory ([string]$packageManifest.entrypoint)
    if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
        throw "Installed StoryForge executable is missing: $entrypoint"
    }

    $pointer = [ordered]@{
        schema_version = 1
        version = $version
        app_directory = $appDirectory
        entrypoint = $entrypoint
        installed_at_utc = [DateTime]::UtcNow.ToString('o')
        source_release = [string]$release.tag_name
    }
    $pointerPath = Join-Path $InstallRoot 'current.json'
    $desktopLauncherPath = Join-Path $InstallRoot 'Start-StoryForge.cmd'

    if ($Role -eq 'Hub') {
        if ($FreshReplacementHost) {
            [void](Assert-FreshReplacementHostState `
                -InstallRoot $InstallRoot `
                -DataRoot $DataRoot `
                -TaskName $TaskName `
                -Port $Port `
                -RequireEmptyData)
        }
        [System.IO.Directory]::CreateDirectory($DataRoot) | Out-Null
        if (-not $dataHasContent) {
            $selfTestRoot = Join-Path $stageRoot 'startup-self-test'
            $previousDataRoot = [Environment]::GetEnvironmentVariable('STORYFORGE_DATA_DIR', 'Process')
            try {
                $env:STORYFORGE_DATA_DIR = $DataRoot
                & $entrypoint --startup-self-test $selfTestRoot
                if ($LASTEXITCODE -ne 0) {
                    throw 'StoryForge startup self-test failed while initializing the new Hub DataRoot.'
                }
                $selfTest = Get-Content -LiteralPath (Join-Path $selfTestRoot 'startup-self-test.json') -Raw -Encoding UTF8 | ConvertFrom-Json
                $catalogPath = Join-Path $DataRoot 'storyforge-catalog.sqlite3'
                $catalogReady = (Test-Path -LiteralPath $catalogPath -PathType Leaf) -and (Get-Item -LiteralPath $catalogPath).Length -gt 0
                if (
                    -not [bool]$selfTest.ok -or
                    [string]$selfTest.status -ne 'passed' -or
                    [string]$selfTest.app_version -ne $version -or
                    -not $catalogReady
                ) {
                    throw 'StoryForge startup self-test result is invalid.'
                }
            }
            finally {
                if ($null -eq $previousDataRoot) {
                    Remove-Item Env:STORYFORGE_DATA_DIR -ErrorAction SilentlyContinue
                }
                else {
                    $env:STORYFORGE_DATA_DIR = $previousDataRoot
                }
            }
            if ($FreshReplacementHost) {
                $freshInitializedDataState = @(
                    Get-FreshReplacementDataState -DataRoot $DataRoot
                )
                if ($freshInitializedDataState.Count -eq 0) {
                    throw 'Fresh replacement startup self-test created no DataRoot state.'
                }
            }
        }

        if ($RestoreHubData) {
            Write-Host 'Downloading the authoritative latest Hub snapshot...'
            $hubRelease = Invoke-GhJson -Arguments @('api', "repos/$Repo/releases/tags/hub-state-latest")
            if (-not [bool]$hubRelease.prerelease -or [bool]$hubRelease.draft) {
                throw 'hub-state-latest must be a published prerelease.'
            }
            $hubName = 'StoryForge-Hub-Latest.sfbak'
            $hubManifestName = 'StoryForge-Hub-Latest.manifest.json'
            $hubAssets = @($hubRelease.assets | Where-Object { [string]$_.name -eq $hubName })
            $hubManifestAssets = @($hubRelease.assets | Where-Object { [string]$_.name -eq $hubManifestName })
            if ($hubAssets.Count -ne 1 -or $hubManifestAssets.Count -ne 1) {
                throw 'hub-state-latest must contain exactly one snapshot and one manifest.'
            }
            $hubDownload = Join-Path $stageRoot 'hub-state'
            [System.IO.Directory]::CreateDirectory($hubDownload) | Out-Null
            Invoke-Gh -Arguments @(
                'release', 'download', 'hub-state-latest', '--repo', $Repo,
                '--dir', $hubDownload, '--pattern', $hubName,
                '--pattern', $hubManifestName, '--clobber'
            )
            $hubSnapshot = Join-Path $hubDownload $hubName
            $hubSidecar = Join-Path $hubDownload $hubManifestName
            Assert-GitHubAsset -Asset $hubAssets[0] -Path $hubSnapshot
            Assert-GitHubAsset -Asset $hubManifestAssets[0] -Path $hubSidecar
            $hubManifest = Get-Content -LiteralPath $hubSidecar -Raw -Encoding UTF8 | ConvertFrom-Json
            Assert-SidecarFile -Manifest $hubManifest -PayloadPath $hubSnapshot -ExpectedFilename $hubName
            if ($FreshReplacementHost) {
                [void](Assert-FreshReplacementHostState `
                    -InstallRoot $InstallRoot `
                    -DataRoot $DataRoot `
                    -TaskName $TaskName `
                    -Port $Port `
                    -ExpectedDataState $freshInitializedDataState)
            }
            $previousDataRoot = [Environment]::GetEnvironmentVariable('STORYFORGE_DATA_DIR', 'Process')
            try {
                $env:STORYFORGE_DATA_DIR = $DataRoot
                $rawRestore = & $entrypoint --restore-hub-backup $hubSnapshot 2>&1
                $restoreExitCode = $LASTEXITCODE
                $restoreText = ($rawRestore | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
                if ($restoreExitCode -ne 0) {
                    throw "Verified Hub snapshot restore failed ($restoreExitCode). Existing data was not silently accepted.`n$restoreText"
                }
                try {
                    $restoreResult = $restoreText | ConvertFrom-Json
                }
                catch {
                    throw 'Verified Hub snapshot restore did not return valid JSON.'
                }
                if (-not [bool]$restoreResult.restored -or -not [bool]$restoreResult.requires_restart) {
                    throw 'Verified Hub snapshot restore did not confirm restored=true and requires_restart=true.'
                }
            }
            finally {
                if ($null -eq $previousDataRoot) {
                    Remove-Item Env:STORYFORGE_DATA_DIR -ErrorAction SilentlyContinue
                }
                else {
                    $env:STORYFORGE_DATA_DIR = $previousDataRoot
                }
            }
        }

        $hubLauncher = Join-Path $InstallRoot 'Start-StoryForge-Hub.ps1'
        $launcherLines = @(
            '$ErrorActionPreference = ''Stop''',
            '$env:STORYFORGE_DEPLOYMENT_ROLE = ''Hub''',
            "`$env:STORYFORGE_DATA_DIR = '$DataRoot'",
            'Remove-Item Env:STORYFORGE_FROZEN_HUB_DATA_ROOT -ErrorAction SilentlyContinue',
            'Remove-Item Env:STORYFORGE_PORTABLE_MODE -ErrorAction SilentlyContinue',
            "& '$entrypoint' --web --web-host 0.0.0.0 --web-port $Port",
            'exit $LASTEXITCODE'
        )
        [System.IO.File]::WriteAllLines($hubLauncher, $launcherLines, [System.Text.Encoding]::ASCII)
        $powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $taskAction = New-ScheduledTaskAction -Execute $powerShell -Argument ("-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$hubLauncher`"") -WorkingDirectory $InstallRoot
        $taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $taskPrincipal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
        $taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Description "StoryForge Hub on private LAN TCP $Port." | Out-Null
        $hubTaskCreated = $true

        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private -RemoteAddress LocalSubnet -Description 'Allow StoryForge employee computers on the private local network.' | Out-Null
        $hubFirewallRuleCreated = $true

        Start-ScheduledTask -TaskName $TaskName
        $webHealthUrl = "http://127.0.0.1:$Port/web/api/health"
        $hubHealthUrl = "http://127.0.0.1:$Port/health"
        $healthy = $false
        $lastHealthDetail = 'waiting for first verified Hub backup'
        $backupReadyWait = [Diagnostics.Stopwatch]::StartNew()
        while ($backupReadyWait.Elapsed.TotalMinutes -lt 10) {
            Start-Sleep -Seconds 1
            try {
                $health = Invoke-RestMethod -Uri $webHealthUrl -TimeoutSec 3
                $hubHealth = Invoke-RestMethod -Uri $hubHealthUrl -TimeoutSec 3
                $lastHealthDetail = "backup=$($health.data.backup.state); ready=$([bool]$health.data.backup.ready); operational=$([bool]$health.data.backup.operational)"
                if (
                    [bool]$health.ok -and
                    [string]$health.data.service -eq 'storyforge-web' -and
                    [string]$health.data.version -eq $version -and
                    [bool]$health.data.backup.available -and
                    [bool]$health.data.backup.enabled -and
                    [bool]$health.data.backup.running -and
                    [bool]$health.data.backup.ready -and
                    [bool]$health.data.backup.operational -and
                    -not [bool]$health.data.backup.has_error -and
                    [bool]$hubHealth.ok -and
                    [string]$hubHealth.service -eq 'storyforge-hub' -and
                    [string]$hubHealth.app_version -eq $version -and
                    [int]$hubHealth.protocol_version -gt 0 -and
                    [int]$hubHealth.schema_version -gt 0 -and
                    [string]$hubHealth.site.id -and
                    @($hubHealth.device_capability_fields) -contains 'device_config_sync'
                ) {
                    $healthy = $true
                    break
                }
                if ([bool]$health.data.backup.has_error) {
                    break
                }
            }
            catch {
                $lastHealthDetail = $_.Exception.Message
            }
        }
        if (-not $healthy) {
            throw "StoryForge Hub did not pass web, RPC and verified-backup health verification: $webHealthUrl; $hubHealthUrl; $lastHealthDetail"
        }
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop)
        $listenerPids = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
        if ($listenerPids.Count -ne 1) {
            throw "StoryForge Hub health responded, but TCP port $Port does not have exactly one owning process."
        }
        $listenerProcess = Get-Win32ProcessById -ProcessId ([int]$listenerPids[0])
        if (-not [string]$listenerProcess.ExecutablePath) {
            throw "Unable to verify the executable listening on TCP port $Port."
        }
        $expectedExecutable = [System.IO.Path]::GetFullPath($entrypoint)
        $actualExecutable = [System.IO.Path]::GetFullPath([string]$listenerProcess.ExecutablePath)
        $commandLine = [string]$listenerProcess.CommandLine
        $portPattern = '(?i)(?:--web-port(?:=|\s+))' + [Regex]::Escape([string]$Port) + '(?:\s|$)'
        if (
            $actualExecutable -ne $expectedExecutable -or
            $commandLine -notmatch '(?i)(?:^|\s)--web(?:\s|$)' -or
            $commandLine -notmatch $portPattern
        ) {
            throw "Unknown process is listening on TCP port $Port. PID=$($listenerProcess.ProcessId); exe=$actualExecutable"
        }
        Write-DesktopLauncher -Destination $desktopLauncherPath -Entrypoint $entrypoint -Role 'Hub' -DataRoot $DataRoot
        Write-DeploymentPointer -Destination $pointerPath -Value $pointer
        $hubProvisioningComplete = $true
        Write-Host "StoryForge Hub $version is ready: http://127.0.0.1:$Port/"
    }
    else {
        # Employee package validation has completed before publishing the pointer.
        Write-DesktopLauncher -Destination $desktopLauncherPath -Entrypoint $entrypoint -Role 'Employee'
        Write-DeploymentPointer -Destination $pointerPath -Value $pointer
        Write-Host "StoryForge Employee $version is installed: $entrypoint"
        Write-Host "Start it with: $(Join-Path $InstallRoot 'Start-StoryForge.cmd')"
    }
}
catch {
    $deploymentError = $_
    if ($Role -eq 'Hub' -and -not $hubProvisioningComplete) {
        if ($hubTaskCreated) {
            try {
                Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            }
            catch {
                # Best-effort rollback continues with task removal.
            }
            try {
                Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
            }
            catch {
                Write-Warning "Unable to remove the newly created scheduled task '$TaskName': $($_.Exception.Message)"
            }
        }
        if ($hubFirewallRuleCreated) {
            try {
                Remove-NetFirewallRule -DisplayName $ruleName -ErrorAction Stop
            }
            catch {
                Write-Warning "Unable to remove the newly created firewall rule '$ruleName': $($_.Exception.Message)"
            }
        }
    }
    throw $deploymentError
}
finally {
    if ($stageRoot -and (Test-Path -LiteralPath $stageRoot -PathType Container)) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
}
