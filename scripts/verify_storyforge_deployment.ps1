[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Hub', 'Employee')]
    [string]$Role,
    [string]$InstallRoot = '',
    [string]$DataRoot = '',
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [string]$TaskName = 'StoryForge Hub'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-FixedPath {
    param([string]$Value, [string]$Label)
    if (-not [System.IO.Path]::IsPathRooted($Value)) {
        throw "$Label must be an absolute path."
    }
    $full = [System.IO.Path]::GetFullPath($Value).TrimEnd('\')
    if ($full -match '[^\x00-\x7F]' -or $full -notmatch '^[A-Za-z]:\\(?:[A-Za-z0-9_. -]+\\)*[A-Za-z0-9_. -]+$') {
        throw "$Label must use a safe ASCII-only local path."
    }
    $volumeRoot = [System.IO.Path]::GetPathRoot($full)
    if ($full.TrimEnd('\') -eq $volumeRoot.TrimEnd('\')) {
        throw "$Label cannot be a drive root."
    }
    return $full
}

function Get-Win32ProcessById {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    return Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
}

$checks = New-Object System.Collections.Generic.List[object]
function Add-Check {
    param([string]$Name, [bool]$Ok, [string]$Detail)
    $script:checks.Add([pscustomobject]@{
        name = $Name
        ok = $Ok
        detail = $Detail
    }) | Out-Null
}

if (-not $InstallRoot) {
    $InstallRoot = if ($Role -eq 'Hub') { 'D:\StoryForgeHub' } else { 'D:\StoryForge' }
}
$InstallRoot = Get-FixedPath -Value $InstallRoot -Label 'InstallRoot'
$pointer = $null
$entrypoint = ''
$pointerVersion = ''
$pointerPath = Join-Path $InstallRoot 'current.json'
if (-not (Test-Path -LiteralPath $pointerPath -PathType Leaf)) {
    Add-Check -Name 'deployment_pointer' -Ok $false -Detail "Missing: $pointerPath"
}
else {
    try {
        $pointer = Get-Content -LiteralPath $pointerPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $pointerVersion = [string]$pointer.version
        $entrypoint = Get-FixedPath -Value ([string]$pointer.entrypoint) -Label 'entrypoint'
        $appDirectory = Get-FixedPath -Value ([string]$pointer.app_directory) -Label 'app_directory'
        $appPrefix = $InstallRoot.TrimEnd('\') + '\'
        $insideInstallRoot = $appDirectory.StartsWith($appPrefix, [System.StringComparison]::OrdinalIgnoreCase)
        Add-Check -Name 'app_location' -Ok $insideInstallRoot -Detail $appDirectory
        $exeExists = Test-Path -LiteralPath $entrypoint -PathType Leaf
        Add-Check -Name 'entrypoint' -Ok $exeExists -Detail $entrypoint
        $internalPath = Join-Path $appDirectory 'storyforge-update.json'
        if (Test-Path -LiteralPath $internalPath -PathType Leaf) {
            $internal = Get-Content -LiteralPath $internalPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $manifestOk = [string]$internal.version -eq [string]$pointer.version -and [string]$internal.entrypoint -eq [System.IO.Path]::GetFileName($entrypoint)
            Add-Check -Name 'internal_manifest' -Ok $manifestOk -Detail "version=$($internal.version)"
        }
        else {
            Add-Check -Name 'internal_manifest' -Ok $false -Detail "Missing: $internalPath"
        }
    }
    catch {
        Add-Check -Name 'deployment_pointer' -Ok $false -Detail $_.Exception.Message
    }
}

if ($Role -eq 'Hub') {
    if (-not $DataRoot) {
        $DataRoot = Join-Path $InstallRoot 'Data'
    }
    $DataRoot = Get-FixedPath -Value $DataRoot -Label 'DataRoot'
    $catalog = Join-Path $DataRoot 'storyforge-catalog.sqlite3'
    $catalogOk = (Test-Path -LiteralPath $catalog -PathType Leaf) -and (Get-Item -LiteralPath $catalog).Length -gt 0
    Add-Check -Name 'hub_catalog' -Ok $catalogOk -Detail $catalog

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $taskOk = $null -ne $task -and [string]$task.State -in @('Ready', 'Running')
    Add-Check -Name 'scheduled_task' -Ok $taskOk -Detail $(if ($task) { [string]$task.State } else { 'missing' })

    $ruleName = "StoryForge Hub $Port (Private LAN)"
    $rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    $firewallOk = $false
    $firewallDetail = 'missing'
    if ($rule) {
        $portFilter = $rule | Get-NetFirewallPortFilter
        $addressFilter = $rule | Get-NetFirewallAddressFilter
        $firewallOk = [string]$rule.Enabled -eq 'True' -and [string]$rule.Profile -match 'Private' -and [string]$portFilter.Protocol -eq 'TCP' -and [string]$portFilter.LocalPort -eq [string]$Port -and (@($addressFilter.RemoteAddress) -contains 'LocalSubnet')
        $firewallDetail = "enabled=$($rule.Enabled); profile=$($rule.Profile); port=$($portFilter.LocalPort); remote=$($addressFilter.RemoteAddress -join ',')"
    }
    Add-Check -Name 'private_firewall' -Ok $firewallOk -Detail $firewallDetail

    $healthUrl = "http://127.0.0.1:$Port/web/api/health"
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
        $healthOk = (
            [bool]$health.ok -and
            [string]$health.data.service -eq 'storyforge-web' -and
            [string]$health.data.version -eq $pointerVersion -and
            [bool]$health.data.backup.available -and
            [bool]$health.data.backup.enabled -and
            [bool]$health.data.backup.running -and
            -not [bool]$health.data.backup.has_error
        )
        Add-Check -Name 'hub_health' -Ok $healthOk -Detail "$healthUrl; version=$($health.data.version); backup=$($health.data.backup.state)"
    }
    catch {
        Add-Check -Name 'hub_health' -Ok $false -Detail $_.Exception.Message
    }

    try {
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop)
        $listenerPids = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
        if ($listenerPids.Count -ne 1) {
            Add-Check -Name 'hub_listener' -Ok $false -Detail "Expected one owner for TCP $Port; found $($listenerPids.Count)."
        }
        else {
            $listenerProcess = Get-Win32ProcessById -ProcessId ([int]$listenerPids[0])
            $actualExecutable = if ([string]$listenerProcess.ExecutablePath) { [System.IO.Path]::GetFullPath([string]$listenerProcess.ExecutablePath) } else { '' }
            $expectedExecutable = if ($entrypoint) { [System.IO.Path]::GetFullPath($entrypoint) } else { '' }
            $commandLine = [string]$listenerProcess.CommandLine
            $portPattern = '(?i)(?:--web-port(?:=|\s+))' + [Regex]::Escape([string]$Port) + '(?:\s|$)'
            $listenerOk = (
                $actualExecutable -and
                $expectedExecutable -and
                $actualExecutable -eq $expectedExecutable -and
                $commandLine -match '(?i)(?:^|\s)--web(?:\s|$)' -and
                $commandLine -match $portPattern
            )
            Add-Check -Name 'hub_listener' -Ok ([bool]$listenerOk) -Detail "pid=$($listenerProcess.ProcessId); exe=$actualExecutable"
        }
    }
    catch {
        Add-Check -Name 'hub_listener' -Ok $false -Detail $_.Exception.Message
    }
}

$failed = @($checks | Where-Object { -not $_.ok })
$result = [ordered]@{
    ok = $failed.Count -eq 0
    role = $Role
    install_root = $InstallRoot
    checked_at_utc = [DateTime]::UtcNow.ToString('o')
    checks = @($checks)
}
$result | ConvertTo-Json -Depth 6
if ($failed.Count -gt 0) {
    exit 1
}
