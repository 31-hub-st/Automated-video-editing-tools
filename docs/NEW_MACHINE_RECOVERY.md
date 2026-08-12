# StoryForge 新电脑部署与 Hub 迁移

这套流程用于更换 Codex 账号、主电脑或网络后，从 GitHub 私有仓库完整恢复程序和 Hub 固定资料。它不会把员工素材、成品或缓存上传到 GitHub。

## GitHub 中保存什么

- 正式程序：最新稳定版 Release 中的 `StoryForge-<版本>-update.zip` 与同名 manifest。普通用户下载正式更新 ZIP，不下载 GitHub 自动生成的 Source code/源码 ZIP；正式更新 ZIP 根目录直接包含一键恢复入口和它复用的脚本。
- Hub 固定资料：预发布标签 `hub-state-latest` 中仅保留：
  - `StoryForge-Hub-Latest.sfbak`
  - `StoryForge-Hub-Latest.manifest.json`
- 源码、测试、部署脚本和本文档：仓库 `main` 分支。

`hub-state-latest` 每次发布都会覆盖同名资产，不累计日期备份。仓库必须始终为私有；能读取该仓库的人也能下载 Hub 资料快照。

## 不会迁移的内容

- 员工电脑的视频素材、背景音乐、最终 MP4/MP3 和本地缓存。
- 各电脑本地渲染队列和临时文件。
- 换机后无法解密的 Windows DPAPI 密文。大模型、TTS 等 API Key 必须在新 Hub 上重新填写。
- 员工电脑登录状态；员工在新电脑上仍需使用账号和密码登录一次。

## 旧 Hub：发布最新固定资料

在旧 Hub 电脑克隆/更新本仓库后运行：

```powershell
Set-Location 'C:\path\to\Automated-video-editing-tools'
.\scripts\publish_hub_snapshot.ps1 -HubRoot D:\StoryForgeHub
```

脚本通过仓库内受测试的备份命令创建一致性快照，校验 SHA-256 后覆盖 `hub-state-latest` 的两个资产。它不会修改小说库数据库，也不要求关闭正在运行的 Hub。

脚本不读取 `D:\StoryForgeHub\current.json`，也不会根据旧程序版本猜测其命令能力。默认始终从本仓库通过 `python -m storyforge.main --create-hub-backup` 创建快照；旧 Hub 电脑发布快照时需要 Python 3.11 或 3.12（新电脑只恢复时不需要 Python）。系统找不到合适 Python 时可显式传入 `-PythonExe D:\Python312\python.exe`。只有已经人工确认某个 EXE 支持该命令时，才使用 `-StoryForgeExe`。

发布完成后，可在 GitHub Releases 中确认预发布 `hub-state-latest` 只包含两个资产。

## 新电脑：一次性准备

### 推荐：全新 Hub 一键恢复

正式更新 ZIP 完整解压后的根目录提供醒目的中文入口：

```text
一键恢复StoryForge-Hub.cmd
```

它仅限全新 Hub：自动申请 UAC 管理员权限，使用 Windows PowerShell 5.1 与 `ExecutionPolicy Bypass`，缺少 GitHub CLI 时通过 `winget` 安装，在浏览器完成 GitHub 登录并核验私有仓库权限，然后复用本页受支持的 `bootstrap_storyforge.ps1` 与 `verify_storyforge_deployment.ps1`。

脚本固定安装到 `D:\StoryForgeHub`，正式 DataRoot 为 `D:\StoryForgeHub\Data`，端口为 `8765`。只要安装根包含旧程序或其他文件、DataRoot 非空、`StoryForge Hub` 任务已存在、8765 已监听或 StoryForge 正式进程仍在运行，就会中文拒绝；不会合并或覆盖已有数据库。仅下载最新程序不能恢复业务数据；小说、账号、口令和制作记录来自私有 `hub-state-latest` 快照。成功后会显示局域网地址、原地址 `10.0.0.225` 的固定局域网 IP 建议与 DPAPI 提示并打开网页，失败时保留中文摘要和本地日志。

现有 Hub 原地升级不属于换机恢复，绝不能对它运行一键恢复或 bootstrap。若旧机只有参数化 `Start-StoryForgeHub.ps1` 计划任务而没有 `current.json`，先通过正式 Release 摘要、sidecar、archive、内部 manifest 和完整目录校验链安装新 `App-<version>`，再按 [Windows 部署说明](DEPLOYMENT_WINDOWS.md) 使用 `repair_storyforge_hub_launcher.ps1 -TargetAppDirectory <新 App 目录>`。该入口不读取或修改 SQLite，不改 DataRoot，不启停服务。

最短操作与安全边界见 [StoryForge Hub 新电脑一键恢复](ONE_CLICK_HUB_RECOVERY.md)。需要自定义路径或人工操作时，继续使用下方手动流程。

1. 安装 GitHub CLI，并登录有私有仓库权限的 GitHub 账号：

   ```powershell
   gh auth login --hostname github.com
   ```

2. 克隆私有仓库：

   ```powershell
   gh repo clone 31-hub-st/Automated-video-editing-tools D:\StoryForgeSource
   Set-Location D:\StoryForgeSource
   ```

本地安装路径必须是固定磁盘上的纯 ASCII 路径，例如 `D:\StoryForgeHub`、`E:\StoryForge`。不要使用中文目录、U 盘、网络盘或 ZIP 内直接运行。

## 新 Hub：程序与固定资料一起恢复

用“以管理员身份运行”的 Windows PowerShell 执行：

```powershell
.\scripts\bootstrap_storyforge.ps1 `
  -Role Hub `
  -InstallRoot D:\StoryForgeHub `
  -DataRoot D:\StoryForgeHub\Data `
  -RestoreHubData `
  -ReplaceExistingData
```

脚本会：

1. 从 GitHub `latest` 稳定 Release 下载正式程序；
2. 同时校验 GitHub 原生 digest、外部 manifest、ZIP SHA-256/大小和内部 manifest；
3. 安装到 `D:\StoryForgeHub\App-<版本>`；
4. 初始化空数据目录，再离线恢复 `hub-state-latest`；
5. 注册 `StoryForge Hub` 登录计划任务；
6. 仅对 Windows“专用网络 + 本地子网”开放 Hub 端口；
7. 启动 Hub 并检查 `/web/api/health`。

当前采用“管理员登录后自动启动”，以保证 Windows DPAPI 加密的服务密钥仍能由原账号解密。因此 Hub 电脑重启后，需要登录安装 Hub 时使用的 Windows 管理员账号；登录完成后不必打开 StoryForge 窗口。

如果 `DataRoot` 已含文件，脚本默认立即拒绝，避免覆盖错误电脑。确认要用 GitHub 快照完整替换时，才增加：

```powershell
-ReplaceExistingData
```

恢复语义是“以快照为准整体替换”，不是合并。恢复管理的范围包括小说、平台绑定、全部口令、成员、制作记录、团队预设和备份允许的附件。

完成后重新填写新电脑无法解密的 API Key，并检查：

```powershell
.\scripts\verify_storyforge_deployment.ps1 `
  -Role Hub `
  -InstallRoot D:\StoryForgeHub `
  -DataRoot D:\StoryForgeHub\Data
```

## 员工电脑：只安装程序

员工电脑不恢复 Hub 数据、不注册 Hub 服务、不接管主机资料：

```powershell
.\scripts\bootstrap_storyforge.ps1 `
  -Role Employee `
  -InstallRoot D:\StoryForge

.\scripts\verify_storyforge_deployment.ps1 `
  -Role Employee `
  -InstallRoot D:\StoryForge
```

安装后双击安装根目录的 `Start-StoryForge.cmd`，输入 Hub 地址、员工账号和密码。视频素材、音乐和输出目录仍由员工在自己电脑选择。

## 安全边界与故障处理

- 程序 Release 和 Hub 数据快照是两条独立通道；更新程序不会覆盖 DataRoot。
- 下载、校验或解压任一步失败，脚本不会注册服务，也不会恢复数据库。
- 新 DataRoot 会先通过正式程序 `--startup-self-test` 初始化，之后才执行离线恢复。
- 恢复前会检查同名计划任务、StoryForge 相关进程和目标端口；任一仍在使用时会拒绝继续，不能让 SQLite 写入进程与恢复并发。
- 启动后必须同时满足版本一致、Hub 备份服务可用且运行正常，并确认目标端口确实由本次安装的 EXE 监听。
- GitHub 无法访问时不要绕过校验复制不明 ZIP；恢复网络后重试。

## 修改部署脚本后的本地检查

这些检查不会启动 Hub、恢复数据库或生成备份：

```powershell
py -3.12 -m unittest tests.test_one_click_hub_recovery -v
py -3.12 -m unittest tests.test_bootstrap_contract -v
py -3.12 -m unittest `
  tests.test_startup.StartupDiagnosticsTests.test_release_build_copies_current_user_worker_service_scripts `
  tests.test_startup.StartupDiagnosticsTests.test_windows_release_scripts_have_powershell_51_safe_encoding -v

$failed = $false
foreach ($file in @(
  'scripts\restore_storyforge_hub_new_machine.ps1',
  'scripts\bootstrap_storyforge.ps1',
  'scripts\publish_hub_snapshot.ps1',
  'scripts\verify_storyforge_deployment.ps1',
  'scripts\build_exe.ps1'
)) {
  $tokens = $null
  $errors = $null
  [System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path $file), [ref]$tokens, [ref]$errors
  ) | Out-Null
  if ($errors.Count) { $failed = $true; $errors }
}
if ($failed) { exit 1 }
```

## 面向 Codex 的接手方式

换 Codex 账号或电脑后，可直接让 Codex：

> 读取私有仓库 `31-hub-st/Automated-video-editing-tools` 的 `AGENTS.md` 和 `docs/NEW_MACHINE_RECOVERY.md`，按正式 Release 与 `hub-state-latest` 在这台电脑部署 StoryForge。不得手改数据库，已有 DataRoot 未经我确认不得覆盖。

Codex 仍需要本机 GitHub CLI 已完成私有仓库授权；GitHub 登录权限不会随源码复制。
