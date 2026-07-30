[CmdletBinding()]
param(
    [string]$ExecutablePath = '',
    [string]$TaskName = 'StoryForge Local Worker',
    [int]$RequiredProtocolVersion = 2,
    [switch]$RunWorker,
    [switch]$NoHealthWait,
    [switch]$Quiet
)

if ([string]::IsNullOrWhiteSpace($ExecutablePath)) {
    $executableCandidates = @(
        (Join-Path $PSScriptRoot 'StoryForge Studio.exe'),
        (Join-Path (Split-Path -Parent $PSScriptRoot) 'StoryForge Studio.exe')
    )
    $ExecutablePath = $executableCandidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
}

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

trap {
    Write-Host ("启用失败：" + $_.Exception.Message) -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
    throw '没有找到 StoryForge Studio.exe。请保留完整发布文件夹；维修脚本应位于 EXE 同级或 admin-tools 子文件夹。'
}
$resolvedExecutable = (Resolve-Path -LiteralPath $ExecutablePath).Path
if ([IO.Path]::GetFileName($resolvedExecutable) -ne 'StoryForge Studio.exe') {
    throw '请选择完整发布文件夹中的 StoryForge Studio.exe。'
}
if ($RequiredProtocolVersion -lt 1) {
    throw '本机制作服务协议版本无效。'
}

if ($RunWorker) {
    # A desktop window may temporarily own the local worker port.  Keep the
    # login task alive, but do not start a second renderer against the same
    # queue/database.  As soon as the desktop closes, this task takes over.
    while ($true) {
        $desktopWorkerFound = $false
        foreach ($port in 18765..18770) {
            try {
                $response = Invoke-RestMethod `
                    -Uri "http://127.0.0.1:$port/worker/api/health" `
                    -TimeoutSec 1
                $roleProperty = $response.data.PSObject.Properties['worker_role']
                $role = $(if ($null -ne $roleProperty) { [string]$roleProperty.Value } else { '' })
                if ($response.ok -and $response.data.service -eq 'storyforge-local-worker' -and $role -eq 'production-workstation') {
                    $desktopWorkerFound = $true
                    break
                }
            }
            catch {
                # An unused discovery port is expected.
            }
        }
        if (-not $desktopWorkerFound) {
            break
        }
        Start-Sleep -Seconds 2
    }
    & $resolvedExecutable --local-worker
    exit $LASTEXITCODE
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$workingDirectory = Split-Path -Parent $resolvedExecutable
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    if ($existing.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$powershellPath = Join-Path $PSHOME 'powershell.exe'
$quotedScript = '"' + $PSCommandPath + '"'
$quotedExecutable = '"' + $resolvedExecutable + '"'
$quotedTaskName = '"' + $TaskName + '"'
$actionArguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File $quotedScript -ExecutablePath $quotedExecutable -TaskName $quotedTaskName -RequiredProtocolVersion $RequiredProtocolVersion -RunWorker -Quiet"
$action = New-ScheduledTaskAction `
    -Execute $powershellPath `
    -Argument $actionArguments `
    -WorkingDirectory $workingDirectory
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal `
    -UserId $identity `
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
    -Description 'StoryForge employee workstation loopback media worker. No Hub server or LAN listener.' `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

if ($NoHealthWait) {
    if (-not $Quiet) {
        Write-Host '本机制作服务已自动启用；无需再次运行此脚本。' -ForegroundColor Green
    }
    exit 0
}

$endpoint = ''
$pendingEndpoint = ''
$incompatibleEndpoint = ''
$incompatibleProtocol = ''
for ($attempt = 0; $attempt -lt 120 -and -not $endpoint; $attempt += 1) {
    Start-Sleep -Milliseconds 500
    foreach ($port in 18765..18770) {
        try {
            $response = Invoke-RestMethod `
                -Uri "http://127.0.0.1:$port/worker/api/health" `
                -TimeoutSec 2
            if ($response.ok -and $response.data.service -eq 'storyforge-local-worker') {
                $candidateEndpoint = "http://127.0.0.1:$port"
                $protocol = 0
                $protocolProperty = $response.data.PSObject.Properties['protocol_version']
                if ($null -ne $protocolProperty) {
                    $protocol = [int]$protocolProperty.Value
                }
                $minimumBrowser = 0
                $minimumBrowserProperty = $response.data.PSObject.Properties['minimum_browser_protocol_version']
                if ($null -ne $minimumBrowserProperty) {
                    $minimumBrowser = [int]$minimumBrowserProperty.Value
                }
                if ($protocol -lt $RequiredProtocolVersion -or $minimumBrowser -gt $RequiredProtocolVersion) {
                    $incompatibleEndpoint = $candidateEndpoint
                    $incompatibleProtocol = $(if ($protocol) { [string]$protocol } else { '旧版/未提供' })
                    continue
                }
                $workerRoleProperty = $response.data.PSObject.Properties['worker_role']
                $workerRole = $(if ($null -ne $workerRoleProperty) { [string]$workerRoleProperty.Value } else { '' })
                if ($response.data.ready -and $workerRole -eq 'production-workstation') {
                    $endpoint = $candidateEndpoint
                    break
                }
                if ($workerRole -eq 'production-workstation') {
                    $pendingEndpoint = $candidateEndpoint
                }
            }
        }
        catch {
            # The frozen executable may still be unpacking or reconnecting Hub.
        }
    }
    if ($attempt -ge 9) {
        $currentTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $currentTask -or $currentTask.State -ne 'Running') {
            break
        }
    }
}

$task = Get-ScheduledTask -TaskName $TaskName
if ($task.State -ne 'Running') {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    throw '本机制作服务没有启动成功。请先打开 StoryForge，用员工账号连接主电脑并绑定这台电脑，然后重新运行本脚本。'
}
if (-not $endpoint -and $pendingEndpoint) {
    if (-not $Quiet) {
        Write-Host "本机制作服务已安装并正在运行：$pendingEndpoint" -ForegroundColor Yellow
        Write-Host '当前暂时没有连上主电脑；主电脑或网络恢复后会自动重连，不需要重复启用。' -ForegroundColor Yellow
        Write-Host "登录任务：$TaskName（当前 Windows 用户）"
    }
    exit 0
}
if (-not $endpoint -and $incompatibleEndpoint) {
    throw "检测到旧版本机制作服务（协议 $incompatibleProtocol）。请关闭旧版 StoryForge，再用当前发布文件夹重新运行本脚本。"
}
if (-not $endpoint) {
    throw '本机制作服务已注册，但 60 秒内没有完成启动。请运行 diagnose_storyforge.cmd，并把桌面生成的诊断报告交给管理员。'
}

if (-not $Quiet) {
    Write-Host "本机制作服务已正常运行：$endpoint" -ForegroundColor Green
    Write-Host "登录任务：$TaskName（当前 Windows 用户，无需管理员权限）"
    Write-Host '现在可以关闭 StoryForge 完整窗口；网页仍会使用这台电脑的素材、配音、FFmpeg/GPU 和输出目录。'
}
