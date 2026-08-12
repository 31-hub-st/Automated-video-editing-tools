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

function Get-StoryForgeFirewallRuleCheck {
    param(
        [Parameter(Mandatory = $true)][string]$RuleName,
        [Parameter(Mandatory = $true)][int]$Port
    )

    $rule = $null
    try {
        $rule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction Stop
    }
    catch {
        $rule = $null
    }

    if ($rule) {
        $portFilter = $rule | Get-NetFirewallPortFilter
        $addressFilter = $rule | Get-NetFirewallAddressFilter
        $ok = [string]$rule.Enabled -eq 'True' -and [string]$rule.Direction -eq 'Inbound' -and [string]$rule.Action -eq 'Allow' -and [string]$rule.Profile -match 'Private' -and [string]$portFilter.Protocol -eq 'TCP' -and [string]$portFilter.LocalPort -eq [string]$Port -and (@($addressFilter.RemoteAddress) -contains 'LocalSubnet')
        return [pscustomobject]@{
            ok = [bool]$ok
            detail = "enabled=$($rule.Enabled); direction=$($rule.Direction); action=$($rule.Action); profile=$($rule.Profile); port=$($portFilter.LocalPort); remote=$($addressFilter.RemoteAddress -join ',')"
        }
    }

    try {
        $policy = New-Object -ComObject HNetCfg.FwPolicy2 -ErrorAction Stop
        $comRule = $policy.Rules.Item($RuleName)
        if ($null -eq $comRule) {
            throw "Firewall rule not found: $RuleName"
        }
        $remoteAddressText = [string]$comRule.RemoteAddresses
        $remoteAddresses = @(
            $remoteAddressText.Split(',') |
                ForEach-Object { $_.Trim() } |
                Where-Object { $_ }
        )
        $hasLocalSubnet = @(
            $remoteAddresses | Where-Object {
                [string]::Equals(
                    [string]$_,
                    'LocalSubnet',
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            }
        ).Count -gt 0
        $profiles = [int]$comRule.Profiles
        $ok = (
            [bool]$comRule.Enabled -and
            [int]$comRule.Direction -eq 1 -and
            [int]$comRule.Action -eq 1 -and
            ($profiles -band 2) -eq 2 -and
            [int]$comRule.Protocol -eq 6 -and
            [string]$comRule.LocalPorts -eq [string]$Port -and
            $hasLocalSubnet
        )
        return [pscustomobject]@{
            ok = [bool]$ok
            detail = "enabled=$($comRule.Enabled); direction=$($comRule.Direction); action=$($comRule.Action); profiles=$profiles; protocol=$($comRule.Protocol); port=$($comRule.LocalPorts); remote=$remoteAddressText"
        }
    }
    catch {
        return [pscustomobject]@{
            ok = $false
            detail = "missing; COM firewall lookup failed: $($_.Exception.Message)"
        }
    }
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
    $firewall = Get-StoryForgeFirewallRuleCheck -RuleName $ruleName -Port $Port
    Add-Check -Name 'private_firewall' -Ok ([bool]$firewall.ok) -Detail ([string]$firewall.detail)

    $webHealthUrl = "http://127.0.0.1:$Port/web/api/health"
    $healthOk = $false
    $healthDetail = 'waiting for first verified Hub backup'
    $backupReadyWait = [Diagnostics.Stopwatch]::StartNew()
    while ($backupReadyWait.Elapsed.TotalMinutes -lt 10) {
        try {
            $health = Invoke-RestMethod -Uri $webHealthUrl -TimeoutSec 5
            $healthOk = (
                [bool]$health.ok -and
                [string]$health.data.service -eq 'storyforge-web' -and
                [string]$health.data.version -eq $pointerVersion -and
                [bool]$health.data.backup.available -and
                [bool]$health.data.backup.enabled -and
                [bool]$health.data.backup.running -and
                [bool]$health.data.backup.ready -and
                [bool]$health.data.backup.operational -and
                -not [bool]$health.data.backup.has_error
            )
            $healthDetail = "$webHealthUrl; version=$($health.data.version); backup=$($health.data.backup.state); ready=$([bool]$health.data.backup.ready); operational=$([bool]$health.data.backup.operational)"
            if ($healthOk -or [bool]$health.data.backup.has_error) {
                break
            }
        }
        catch {
            $healthDetail = $_.Exception.Message
        }
        if ($backupReadyWait.Elapsed.TotalMinutes -lt 10) {
            Start-Sleep -Seconds 1
        }
    }
    Add-Check -Name 'hub_web_health' -Ok $healthOk -Detail $healthDetail

    $hubHealthUrl = "http://127.0.0.1:$Port/health"
    try {
        $hubHealth = Invoke-RestMethod -Uri $hubHealthUrl -TimeoutSec 5
        $hubHealthOk = (
            [bool]$hubHealth.ok -and
            [string]$hubHealth.service -eq 'storyforge-hub' -and
            [string]$hubHealth.app_version -eq $pointerVersion -and
            [int]$hubHealth.protocol_version -gt 0 -and
            [int]$hubHealth.schema_version -gt 0 -and
            [string]$hubHealth.site.id -and
            @($hubHealth.device_capability_fields) -contains 'device_config_sync'
        )
        Add-Check -Name 'hub_rpc_health' -Ok $hubHealthOk -Detail "$hubHealthUrl; version=$($hubHealth.app_version); protocol=$($hubHealth.protocol_version); schema=$($hubHealth.schema_version)"
    }
    catch {
        Add-Check -Name 'hub_rpc_health' -Ok $false -Detail $_.Exception.Message
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
    checks = $checks.ToArray()
}
$result | ConvertTo-Json -Depth 6
if ($failed.Count -gt 0) {
    exit 1
}
