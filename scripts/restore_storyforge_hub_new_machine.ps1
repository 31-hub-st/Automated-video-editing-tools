[CmdletBinding()]
param(
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$Repo = '31-hub-st/Automated-video-editing-tools'
$InstallRoot = 'D:\StoryForgeHub'
$DataRoot = 'D:\StoryForgeHub\Data'
$TaskName = 'StoryForge Hub'
$Port = 8765
$PowerShell51 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$BootstrapScript = Join-Path $PSScriptRoot 'bootstrap_storyforge.ps1'
$VerifyScript = Join-Path $PSScriptRoot 'verify_storyforge_deployment.ps1'
# Keep every gh operation, including the child bootstrap process, on the same
# host that is authenticated and permission-checked below. An inherited
# GH_HOST for a GitHub Enterprise server must not redirect the recovery chain.
$env:GH_HOST = 'github.com'

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Wait-ForOperator {
    param([string]$Prompt = '按 Enter 键关闭此窗口')
    if (-not $NoPause) {
        [void](Read-Host $Prompt)
    }
}

function Start-ElevatedRecovery {
    $arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($NoPause) {
        $arguments += ' -NoPause'
    }
    try {
        Write-Host '需要管理员权限，正在打开 Windows 授权窗口……' -ForegroundColor Cyan
        $process = Start-Process `
            -FilePath $PowerShell51 `
            -ArgumentList $arguments `
            -Verb RunAs `
            -Wait `
            -PassThru
        exit $process.ExitCode
    }
    catch {
        Write-Host ''
        Write-Host '未获得管理员权限，恢复没有开始，也没有修改任何 StoryForge 数据。' -ForegroundColor Red
        Write-Host ("原因：{0}" -f $_.Exception.Message) -ForegroundColor Red
        Wait-ForOperator
        exit 1
    }
}

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ''
    Write-Host ("==> {0}" -f $Message) -ForegroundColor Cyan
}

function Get-GhExecutable {
    $command = Get-Command -Name 'gh.exe' -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = New-Object System.Collections.Generic.List[string]
    if ($env:ProgramFiles) {
        $candidates.Add((Join-Path $env:ProgramFiles 'GitHub CLI\gh.exe'))
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates.Add((Join-Path ${env:ProgramFiles(x86)} 'GitHub CLI\gh.exe'))
    }
    if ($env:LOCALAPPDATA) {
        $candidates.Add((Join-Path $env:LOCALAPPDATA 'Programs\GitHub CLI\gh.exe'))
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return ''
}

function Ensure-GitHubCli {
    $ghPath = Get-GhExecutable
    if ($ghPath) {
        Write-Host ("已找到 GitHub CLI：{0}" -f $ghPath)
        return $ghPath
    }

    $winget = Get-Command -Name 'winget.exe' -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw '未找到 GitHub CLI，也未找到 winget。请从 https://cli.github.com/ 下载并安装 GitHub CLI，安装后重新双击本入口。'
    }

    Write-Step '未检测到 GitHub CLI，正在通过 winget 安装'
    & $winget.Source install `
        --id GitHub.cli `
        --exact `
        --source winget `
        --accept-source-agreements `
        --accept-package-agreements `
        --silent
    if ($LASTEXITCODE -ne 0) {
        throw "winget 安装 GitHub CLI 失败（退出码：$LASTEXITCODE）。请从 https://cli.github.com/ 手动下载安装，安装后重新双击本入口。"
    }

    $ghPath = Get-GhExecutable
    if (-not $ghPath) {
        throw 'GitHub CLI 安装完成，但当前系统仍找不到 gh.exe。请从 https://cli.github.com/ 确认安装，安装后重新双击本入口。'
    }
    $ghDirectory = Split-Path -Parent $ghPath
    if (($env:Path -split ';') -notcontains $ghDirectory) {
        $env:Path = "$ghDirectory;$env:Path"
    }
    return $ghPath
}

function Assert-FreshHubPreflight {
    Write-Step '执行全新 Hub 安全预检'

    if (-not (Test-Path -LiteralPath 'D:\' -PathType Container)) {
        throw '未找到 D: 盘。此一键入口固定安装到 D:\StoryForgeHub，请先准备本地 D: 盘。'
    }

    if (Test-Path -LiteralPath $InstallRoot) {
        if (-not (Test-Path -LiteralPath $InstallRoot -PathType Container)) {
            throw "安装根路径不是文件夹，已拒绝继续：$InstallRoot"
        }
        $installItem = Get-Item -LiteralPath $InstallRoot -Force -ErrorAction Stop
        if (
            ($installItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        ) {
            throw "安装根不能是符号链接或目录联接，已拒绝继续：$InstallRoot"
        }
        $existingInstall = @(
            Get-ChildItem -LiteralPath $InstallRoot -Force -ErrorAction Stop |
                Where-Object {
                    -not (
                        $_.PSIsContainer -and
                        $_.FullName -ieq $DataRoot -and
                        @(
                            Get-ChildItem -LiteralPath $DataRoot -Force -ErrorAction Stop |
                                Select-Object -First 1
                        ).Count -eq 0
                    )
                } |
                Select-Object -First 1
        )
        if ($existingInstall.Count -gt 0) {
            throw "安装根已含旧程序或其他文件，已拒绝覆盖：$InstallRoot。此入口仅限全新 Hub。"
        }
    }

    if (Test-Path -LiteralPath $DataRoot) {
        if (-not (Test-Path -LiteralPath $DataRoot -PathType Container)) {
            throw "正式 DataRoot 不是文件夹，已拒绝继续：$DataRoot"
        }
        $dataItem = Get-Item -LiteralPath $DataRoot -Force -ErrorAction Stop
        if (
            ($dataItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        ) {
            throw "正式 DataRoot 不能是符号链接或目录联接，已拒绝继续：$DataRoot"
        }
        $existingData = @(
            Get-ChildItem -LiteralPath $DataRoot -Force -ErrorAction Stop |
                Select-Object -First 1
        )
        if ($existingData.Count -gt 0) {
            throw "正式 DataRoot 已含文件，已拒绝覆盖：$DataRoot。此入口仅限全新 Hub，不会合并或覆盖现有资料。"
        }
    }

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        throw "已存在计划任务 [$TaskName]，这不是全新 Hub，已拒绝继续。"
    }

    $listeners = @(
        Get-NetTCPConnection `
            -State Listen `
            -LocalPort $Port `
            -ErrorAction SilentlyContinue
    )
    if ($listeners.Count -gt 0) {
        $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
        throw "TCP 端口 $Port 已被进程占用（PID：$($owners -join ',')），已拒绝继续。"
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
        $processSummary = @(
            $storyForgeProcesses |
                ForEach-Object { '{0}:{1}' -f $_.ProcessId, $_.Name }
        ) -join ', '
        throw "检测到正在运行的 StoryForge 正式进程（$processSummary），已拒绝继续。"
    }

    Write-Host '安全预检通过：正式数据目录为空，任务、端口和正式进程均未占用。' -ForegroundColor Green
}

function Ensure-GitHubAuthentication {
    param([Parameter(Mandatory = $true)][string]$GhPath)

    Write-Step '检查 GitHub 登录与私有仓库权限'
    & $GhPath auth status --hostname github.com 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Host '即将打开浏览器，请登录有权访问 StoryForge 私有仓库的 GitHub 账号。'
        & $GhPath auth login --hostname github.com --web --git-protocol https 2>&1 |
            Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "GitHub 浏览器登录失败，退出码：$LASTEXITCODE"
        }
    }

    & $GhPath auth status --hostname github.com 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw 'GitHub 登录状态验证失败。'
    }

    $repoJsonPath = Join-Path $env:TEMP (
        'storyforge-repo-permission-{0}.json' -f [Guid]::NewGuid().ToString('N')
    )
    try {
        & $GhPath repo view $Repo --json nameWithOwner,isPrivate 1> $repoJsonPath 2> $null
        $repoExitCode = $LASTEXITCODE
        if ($repoExitCode -ne 0) {
            throw "当前 GitHub 账号无权读取私有仓库 $Repo。请确认登录账号后重试。"
        }
        $repoInfo = Get-Content -LiteralPath $repoJsonPath -Raw -Encoding UTF8 |
            ConvertFrom-Json
    }
    catch {
        if ($_.Exception.Message -like '当前 GitHub 账号无权读取私有仓库*') {
            throw
        }
        throw 'GitHub 返回的仓库权限信息无法解析，已拒绝继续。'
    }
    finally {
        if (Test-Path -LiteralPath $repoJsonPath -PathType Leaf) {
            Remove-Item -LiteralPath $repoJsonPath -Force -ErrorAction SilentlyContinue
        }
    }
    if (
        [string]$repoInfo.nameWithOwner -ne $Repo -or
        -not [bool]$repoInfo.isPrivate
    ) {
        throw 'GitHub 仓库身份或私有属性不符合预期，已拒绝继续。'
    }
    Write-Host ("私有仓库权限验证通过：{0}" -f $repoInfo.nameWithOwner) -ForegroundColor Green
}

function Invoke-CheckedPowerShellScript {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) {
        throw "缺少必需脚本：$ScriptPath"
    }
    & $PowerShell51 `
        -NoLogo `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $ScriptPath `
        @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage，退出码：$LASTEXITCODE"
    }
}

function Get-LanUrls {
    $addresses = @(
        Get-NetIPAddress `
            -AddressFamily IPv4 `
            -AddressState Preferred `
            -ErrorAction SilentlyContinue |
            Where-Object {
                $_.IPAddress -ne '127.0.0.1' -and
                $_.IPAddress -notlike '169.254.*' -and
                [string]$_.InterfaceAlias -notmatch '(?i)loopback|vEthernet|Hyper-V|WSL|Bluetooth'
            } |
            Select-Object -ExpandProperty IPAddress -Unique |
            Sort-Object
    )
    return @($addresses | ForEach-Object { "http://${_}:$Port/" })
}

if (-not (Test-IsAdministrator)) {
    Start-ElevatedRecovery
}

$transcriptStarted = $false
$exitCode = 1
$localUrl = "http://127.0.0.1:$Port/"
$bootstrapStarted = $false
$bootstrapCompleted = $false
$verifyCompleted = $false
$logPath = '(日志尚未创建)'

try {
    $logDirectory = Join-Path $env:LOCALAPPDATA 'StoryForge\RecoveryLogs'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $logPath = Join-Path $logDirectory (
        'hub-recovery-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    )
    Start-Transcript -Path $logPath -Force | Out-Null
    $transcriptStarted = $true
    Write-Host 'StoryForge Hub 新电脑一键恢复' -ForegroundColor White
    Write-Host ("仓库目录：{0}" -f $RepositoryRoot)
    Write-Host ("恢复日志：{0}" -f $logPath)
    Write-Host '注意：此入口仅限全新 Hub；恢复是以 GitHub 快照为准整体替换，不会合并任何数据库。' -ForegroundColor Yellow

    Assert-FreshHubPreflight
    $ghPath = Ensure-GitHubCli
    Ensure-GitHubAuthentication -GhPath $ghPath

    # winget installation and browser authorization can leave this process
    # waiting for minutes. Bind the destructive restore decision to a fresh
    # observation immediately before invoking bootstrap, not only to the
    # initial preflight.
    Assert-FreshHubPreflight

    Write-Step '下载、校验正式 Release，并恢复唯一 Hub 快照'
    $bootstrapArguments = @(
        '-Role', 'Hub',
        '-Repo', $Repo,
        '-InstallRoot', $InstallRoot,
        '-DataRoot', $DataRoot,
        '-TaskName', $TaskName,
        '-RestoreHubData',
        '-ReplaceExistingData',
        '-FreshReplacementHost',
        '-Port', [string]$Port
    )
    $bootstrapStarted = $true
    Invoke-CheckedPowerShellScript `
        -ScriptPath $BootstrapScript `
        -Arguments $bootstrapArguments `
        -FailureMessage '正式部署或快照恢复失败'
    $bootstrapCompleted = $true

    Write-Step '执行正式部署验证'
    $verifyArguments = @(
        '-Role', 'Hub',
        '-InstallRoot', $InstallRoot,
        '-DataRoot', $DataRoot,
        '-TaskName', $TaskName,
        '-Port', [string]$Port
    )
    Invoke-CheckedPowerShellScript `
        -ScriptPath $VerifyScript `
        -Arguments $verifyArguments `
        -FailureMessage '部署验证失败'
    $verifyCompleted = $true

    $lanUrls = @(Get-LanUrls)
    Write-Host ''
    Write-Host 'StoryForge Hub 恢复成功，正式部署验证已通过。' -ForegroundColor Green
    Write-Host ("本机地址：{0}" -f $localUrl) -ForegroundColor Green
    if ($lanUrls.Count -gt 0) {
        Write-Host '局域网地址：' -ForegroundColor Green
        foreach ($url in $lanUrls) {
            Write-Host ("  {0}" -f $url) -ForegroundColor Green
        }
    }
    else {
        Write-Host '暂未识别到可用局域网 IPv4 地址；联网后可再次查看网卡地址。' -ForegroundColor Yellow
    }
    Write-Host '旧 Hub 地址为 10.0.0.225。若网络规划允许，请在路由器中为新 Hub 保留 10.0.0.225；否则为当前地址设置 DHCP 地址保留（固定局域网 IP），并同步更新员工端连接地址。' -ForegroundColor Yellow
    Write-Host 'DPAPI 提示：换机后旧电脑加密的 API Key 无法解密，请在新 Hub 中重新填写；以后重启须登录本次安装所用的 Windows 管理员账号。' -ForegroundColor Yellow

    try {
        Start-Process -FilePath $localUrl
    }
    catch {
        Write-Host ("浏览器未能自动打开，请手动访问：{0}" -f $localUrl) -ForegroundColor Yellow
    }
    $exitCode = 0
}
catch {
    Write-Host ''
    if ($bootstrapCompleted -and -not $verifyCompleted) {
        Write-Host 'StoryForge Hub 的程序和快照恢复已返回成功，但最终验证失败。' -ForegroundColor Red
        Write-Host 'Hub 可能已经上线；不要再次运行一键恢复，也不要删除 DataRoot。请保留现场并把下方日志交给管理员核查。' -ForegroundColor Yellow
    }
    elseif ($bootstrapStarted) {
        Write-Host 'StoryForge Hub 在部署或快照恢复阶段失败。' -ForegroundColor Red
        Write-Host '流程可能已创建受管目录；不要强行删除或重复覆盖。请先按下方日志核查，确认仍是空白主机后才能重试。' -ForegroundColor Yellow
    }
    else {
        Write-Host 'StoryForge Hub 恢复尚未开始，安全预检或准备步骤失败。修正下方原因后可重新双击入口。' -ForegroundColor Red
    }
    Write-Host '脚本已停止，不会绕过安全检查继续覆盖。' -ForegroundColor Red
    Write-Host ("原因：{0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host ("日志：{0}" -f $logPath) -ForegroundColor Yellow
    $exitCode = 1
}
finally {
    if ($transcriptStarted) {
        try {
            Stop-Transcript | Out-Null
        }
        catch {
        }
    }
}

if ($exitCode -eq 0) {
    Wait-ForOperator -Prompt '按 Enter 键关闭此窗口（网页已尝试打开）'
}
else {
    Wait-ForOperator -Prompt '请记录上方原因和日志路径，然后按 Enter 键关闭'
}
exit $exitCode
