[CmdletBinding()]
param(
    [string]$Repo = '31-hub-st/Automated-video-editing-tools',
    [string]$HubRoot = 'D:\StoryForgeHub',
    [string]$DataRoot = '',
    [string]$Tag = 'hub-state-latest',
    [string]$StoryForgeExe = '',
    [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-SafeFixedPath {
    param([string]$Value, [string]$Label)
    if (-not [System.IO.Path]::IsPathRooted($Value)) {
        throw "$Label must be an absolute local path."
    }
    $full = [System.IO.Path]::GetFullPath($Value).TrimEnd('\')
    if ($full -match '[^\x00-\x7F]' -or $full -notmatch '^[A-Za-z]:\\(?:[A-Za-z0-9_. -]+\\)*[A-Za-z0-9_. -]+$') {
        throw "$Label must use a safe ASCII-only local path: $full"
    }
    $volumeRoot = [System.IO.Path]::GetPathRoot($full)
    if ($full.TrimEnd('\') -eq $volumeRoot.TrimEnd('\')) {
        throw "$Label cannot be a drive root."
    }
    return $full
}

function Get-GhPath {
    $command = Get-Command -Name 'gh' -ErrorAction SilentlyContinue
    if (-not $command) {
        throw 'GitHub CLI (gh) is required.'
    }
    & $command.Source auth status --hostname github.com | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'GitHub CLI is not authenticated. Run: gh auth login --hostname github.com'
    }
    return $command.Source
}

function Invoke-NativeText {
    param([string]$FilePath, [string[]]$Arguments)
    $output = & $FilePath @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    $text = ($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
    if ($exitCode -ne 0) {
        throw "Command failed ($exitCode): $FilePath $($Arguments -join ' ')`n$text"
    }
    return $text
}

function Invoke-GhJson {
    param([string[]]$Arguments)
    return (Invoke-NativeText -FilePath $script:GhPath -Arguments $Arguments) | ConvertFrom-Json
}

function Invoke-Gh {
    param([string[]]$Arguments)
    & $script:GhPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "GitHub CLI command failed: gh $($Arguments -join ' ')"
    }
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

if ($Repo -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    throw 'Repo must use the owner/name form.'
}
if ($Tag -ne 'hub-state-latest') {
    throw 'The supported non-accumulating Hub state tag is hub-state-latest.'
}
$HubRoot = Get-SafeFixedPath -Value $HubRoot -Label 'HubRoot'
if (-not $DataRoot) {
    $DataRoot = Join-Path $HubRoot 'Data'
}
$DataRoot = Get-SafeFixedPath -Value $DataRoot -Label 'DataRoot'
if (-not (Test-Path -LiteralPath $DataRoot -PathType Container)) {
    throw "Hub DataRoot does not exist: $DataRoot"
}
$catalog = Join-Path $DataRoot 'storyforge-catalog.sqlite3'
if (-not (Test-Path -LiteralPath $catalog -PathType Leaf)) {
    throw "Hub catalog does not exist: $catalog"
}
$script:GhPath = Get-GhPath
$repository = Invoke-GhJson -Arguments @('api', "repos/$Repo")
if (-not [bool]$repository.private) {
    throw 'Refusing to publish Hub business data to a public repository.'
}

$workRoot = Join-Path $HubRoot ('.hub-publish-' + [Guid]::NewGuid().ToString('N'))
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$backupCommand = ''
$backupArguments = @()
$backupWorkingDirectory = $HubRoot
$sourceAppVersion = ''

if ($StoryForgeExe) {
    $backupCommand = Get-SafeFixedPath -Value $StoryForgeExe -Label 'StoryForgeExe'
    if (-not (Test-Path -LiteralPath $backupCommand -PathType Leaf)) {
        throw "StoryForgeExe does not exist: $backupCommand"
    }
}

# Do not infer CLI capabilities from an installed version number.  The GitHub
# migration command can be newer than an already-published executable with the
# same semantic version.  Unless the operator explicitly supplies a verified
# executable, use this repository's current source implementation.
if (-not $backupCommand) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'storyforge\main.py') -PathType Leaf)) {
        throw 'Repository source fallback is unavailable: storyforge\main.py is missing.'
    }
    $pythonPrefix = @()
    if ($PythonExe) {
        $backupCommand = Get-SafeFixedPath -Value $PythonExe -Label 'PythonExe'
        if (-not (Test-Path -LiteralPath $backupCommand -PathType Leaf)) {
            throw "PythonExe does not exist: $backupCommand"
        }
    }
    else {
        foreach ($candidate in @(
            (Join-Path $projectRoot '.build-venv\Scripts\python.exe'),
            (Join-Path $projectRoot '.venv\Scripts\python.exe')
        )) {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $backupCommand = $candidate
                break
            }
        }
        if (-not $backupCommand) {
            $pyCommand = Get-Command -Name 'py.exe' -ErrorAction SilentlyContinue
            if ($pyCommand) {
                foreach ($pythonVersion in @('3.12', '3.11')) {
                    & $pyCommand.Source "-$pythonVersion" -c 'import sys; raise SystemExit(0)' 2>$null
                    if ($LASTEXITCODE -eq 0) {
                        $backupCommand = $pyCommand.Source
                        $pythonPrefix = @("-$pythonVersion")
                        break
                    }
                }
            }
            if (-not $backupCommand) {
                $pythonCommand = Get-Command -Name 'python.exe' -ErrorAction SilentlyContinue
                if ($pythonCommand) {
                    $backupCommand = $pythonCommand.Source
                }
            }
        }
    }
    if (-not $backupCommand) {
        throw 'No supported StoryForge backup executable or Python interpreter was found. Use -StoryForgeExe or -PythonExe.'
    }
    $backupArguments = @($pythonPrefix) + @('-m', 'storyforge.main', '--create-hub-backup', $workRoot)
    $backupWorkingDirectory = $projectRoot
}
else {
    $backupArguments = @('--create-hub-backup', $workRoot)
}

[System.IO.Directory]::CreateDirectory($workRoot) | Out-Null
$previousDataRoot = [Environment]::GetEnvironmentVariable('STORYFORGE_DATA_DIR', 'Process')
try {
    Write-Host 'Creating one consistent Hub migration snapshot...'
    $env:STORYFORGE_DATA_DIR = $DataRoot
    Push-Location $backupWorkingDirectory
    try {
        $rawResult = & $backupCommand @backupArguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    $resultText = ($rawResult | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
    if ($exitCode -ne 0) {
        throw "StoryForge Hub snapshot command failed ($exitCode).`n$resultText"
    }
    try {
        $result = $resultText | ConvertFrom-Json
    }
    catch {
        throw 'StoryForge Hub snapshot command did not return valid JSON.'
    }
    if (-not [bool]$result.valid) {
        throw 'StoryForge reported an invalid Hub snapshot.'
    }
    if (-not $sourceAppVersion -and $result.metadata) {
        $sourceAppVersion = [string]$result.metadata.app_version
    }
    if (-not $sourceAppVersion) {
        throw 'StoryForge Hub snapshot result did not identify its source app version.'
    }
    $createdPath = [System.IO.Path]::GetFullPath([string]$result.path)
    $workPrefix = $workRoot.TrimEnd('\') + '\'
    if (-not $createdPath.StartsWith($workPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'StoryForge returned a snapshot outside the isolated publish directory.'
    }
    if (-not (Test-Path -LiteralPath $createdPath -PathType Leaf)) {
        throw "Created Hub snapshot is missing: $createdPath"
    }

    $snapshotName = 'StoryForge-Hub-Latest.sfbak'
    $manifestName = 'StoryForge-Hub-Latest.manifest.json'
    $snapshotPath = Join-Path $workRoot $snapshotName
    if ($createdPath -ne $snapshotPath) {
        Move-Item -LiteralPath $createdPath -Destination $snapshotPath -Force
    }
    $snapshotHash = Get-Sha256 -Path $snapshotPath
    $snapshotSize = (Get-Item -LiteralPath $snapshotPath).Length
    $manifest = [ordered]@{
        schema_version = 1
        purpose = 'github_private_recovery'
        filename = $snapshotName
        sha256 = $snapshotHash
        size_bytes = $snapshotSize
        created_at_utc = [DateTime]::UtcNow.ToString('o')
        source_app_version = $sourceAppVersion
        snapshot_id = [string]$result.id
        catalog_schema_version = $result.catalog_schema_version
        settings_schema_version = $result.settings_schema_version
        content_sha256 = [string]$result.content_sha256
        file_count = $result.file_count
        note = 'Provider API keys protected by Windows DPAPI must be re-entered on another computer.'
    }
    $manifestPath = Join-Path $workRoot $manifestName
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $manifestPath,
        ($manifest | ConvertTo-Json -Depth 5),
        $utf8NoBom
    )

    $release = $null
    # A first publication legitimately returns HTTP 404. Windows PowerShell
    # can promote native stderr to a terminating NativeCommandError while the
    # script-wide preference is Stop, so capture this one probe explicitly.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $existingText = & $script:GhPath api "repos/$Repo/releases/tags/$Tag" 2>$null
        $existingExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($existingExitCode -eq 0) {
        $release = (($existingText | ForEach-Object { [string]$_ }) -join [Environment]::NewLine) | ConvertFrom-Json
        if (-not [bool]$release.prerelease -or [bool]$release.draft) {
            throw "$Tag exists but is not a published prerelease."
        }
    }
    elseif ($existingExitCode -ne 1) {
        throw "Unable to check the existing $Tag release (exit $existingExitCode)."
    }
    else {
        Invoke-Gh -Arguments @(
            'release', 'create', $Tag, '--repo', $Repo, '--target', 'main',
            '--title', 'StoryForge Hub latest state', '--prerelease',
            '--notes', 'Single replaceable private Hub recovery snapshot. Do not make this repository public.'
        )
    }

    Write-Host 'Uploading the two replaceable Hub recovery assets...'
    Invoke-Gh -Arguments @(
        'release', 'upload', $Tag, '--repo', $Repo, '--clobber',
        $snapshotPath, $manifestPath
    )

    $release = Invoke-GhJson -Arguments @('api', "repos/$Repo/releases/tags/$Tag")
    $allowedNames = @($snapshotName, $manifestName)
    foreach ($asset in @($release.assets)) {
        if ($allowedNames -notcontains [string]$asset.name) {
            Invoke-Gh -Arguments @(
                'api', '--method', 'DELETE',
                "repos/$Repo/releases/assets/$($asset.id)"
            )
        }
    }

    $release = Invoke-GhJson -Arguments @('api', "repos/$Repo/releases/tags/$Tag")
    $assets = @($release.assets)
    if ($assets.Count -ne 2) {
        throw "$Tag must contain exactly two assets after publication."
    }
    $snapshotAsset = @($assets | Where-Object { [string]$_.name -eq $snapshotName })
    $manifestAsset = @($assets | Where-Object { [string]$_.name -eq $manifestName })
    if ($snapshotAsset.Count -ne 1 -or $manifestAsset.Count -ne 1) {
        throw 'Published Hub recovery asset names are incomplete.'
    }
    if ([string]$snapshotAsset[0].digest -ne "sha256:$snapshotHash") {
        throw 'GitHub native digest does not match the published Hub snapshot.'
    }
    $manifestHash = Get-Sha256 -Path $manifestPath
    if ([string]$manifestAsset[0].digest -ne "sha256:$manifestHash") {
        throw 'GitHub native digest does not match the published Hub manifest.'
    }
    Write-Host "Published private Hub snapshot: $($release.html_url)"
    Write-Host "Snapshot SHA-256: $snapshotHash"
}
finally {
    if ($null -eq $previousDataRoot) {
        Remove-Item Env:STORYFORGE_DATA_DIR -ErrorAction SilentlyContinue
    }
    else {
        $env:STORYFORGE_DATA_DIR = $previousDataRoot
    }
    if (Test-Path -LiteralPath $workRoot -PathType Container) {
        Remove-Item -LiteralPath $workRoot -Recurse -Force
    }
}
