# StoryForge Studio

## 新电脑一键部署

更换 Codex 账号、主电脑或网络后，不需要从旧电脑手工拼装程序。先给 GitHub CLI 授权私有仓库，再让 Codex 读取 [AGENTS.md](AGENTS.md) 和 [新电脑恢复说明](docs/NEW_MACHINE_RECOVERY.md)。

```powershell
gh auth login --hostname github.com
gh repo clone 31-hub-st/Automated-video-editing-tools D:\StoryForgeSource
Set-Location D:\StoryForgeSource

# 新 Hub 主机：管理员 PowerShell
.\scripts\bootstrap_storyforge.ps1 -Role Hub -RestoreHubData -ReplaceExistingData

# 员工制作电脑：只安装程序
.\scripts\bootstrap_storyforge.ps1 -Role Employee -InstallRoot D:\StoryForge
```

正式程序来自 GitHub 最新稳定 Release；Hub 小说库、平台绑定、全部口令、成员和记录来自私有预发布 `hub-state-latest`。两者均进行 SHA-256、大小和 manifest 校验。Hub 快照每次覆盖同名资产，不累计重复文件。API Key 因 Windows 加密机制需要在新主机重新填写；员工素材、成品与缓存不上传 GitHub。

StoryForge Studio 是一套面向小说推文生产的 Windows 本地批量制作系统。它把小说资料、平台口令、配音、字幕、简介卡、视频素材、背景音乐、批次队列和生产记录整合到同一套桌面与网页界面中。

当前正式版本：`v1.0.1`

## 先看这三份文档

- [项目接手与功能嫁接说明](docs/STORYFORGE_INTEGRATION_REPORT.md)：架构、模块、数据模型、接口、流水线、权限、迁移和二次开发边界。
- [Windows 部署与多电脑协同](docs/DEPLOYMENT_WINDOWS.md)：主机、员工制作电脑、数据目录、登录和故障处理。
- [员工快速使用说明](docs/EMPLOYEE_QUICK_START.md)：最短生产流程。

发布证据、包体哈希和验收结果见 [v1.0.1 正式版交付报告](docs/V1.0.1_RELEASE.md)。

## 产品能力

- 导入 TXT、DOCX 或粘贴小说正文，自动检测语种、识别章节并建立小说主档。
- 小说与推广平台、口令长期绑定；每批由员工手动选择口令，默认沿用本机上次选择。
- 勾选一个或多个分集后合并为一段连续正文；全部分集一次制作时不会加入“上集回顾”。
- Hub 主机集中保存小说库、平台、口令、成员、生产记录和大模型配置；员工电脑在本机完成 TTS、字幕和视频渲染。
- 支持本地 Kokoro、Edge TTS 和 Deepgram；默认美式英语，并可按语种读取可用女声。
- 支持舒适 220、推荐 240、快速 260、极快 280 WPM，以及 200–280 WPM 自定义试听。
- 支持整句字幕、单词逐个出现、逐词高亮、稳定字幕、口令卡、安全区、字体/颜色/描边/位置自定义。
- 支持简介卡开关、出现时长、封面图、两种封面故事卡、颜色自定义和内容自适应布局。
- 视频素材由员工选择本机目录；支持递归读取、多素材拼接、循环、镜像、裁切、起点变化和 0.8–3.0 倍固定速度。
- 删除素材原声；背景音乐可关闭、随机/自动选择或手动指定，并在人声和卡片旁白出现时自动压低。
- 默认输出 H.264、1080×1920、60 FPS MP4；音频模式输出 MP3。
- 多批次可以连续提交并排队执行；单条失败会保留原因、跳过并继续后续任务。
- 生产记录按小说、批次、成员、电脑和状态归档；管理员可以刷新、回收或永久删除记录。

## 三种制作方式

| 界面名称 | 内部兼容值 | 最终发布目录 |
| --- | --- | --- |
| 常规视频生成 | `video_and_mp3` | 只生成最终 MP4 |
| 仅生成配音 | `audio_only` | 只生成一份最终 MP3 |
| 已有配音更换素材 | `reuse_audio` | 读取 StoryForge 生成且带字幕索引的 MP3，只生成新 MP4 |

中间 WAV、ASS、清单、质检、命令和错误日志只保存在 `<StoryForgeData>\render-work`，不会混入员工用于发布的输出文件夹。

## 运行结构

```text
Hub 主机
├─ 小说库、平台、口令、成员与生产记录
├─ 文本/大模型服务
├─ 团队网页与 RPC
└─ 3 日滚动备份

员工制作电脑
├─ StoryForge 桌面程序或本机网页
├─ 本机制作服务（Local Worker）
├─ 本机视频、音乐与输出目录
├─ TTS、字幕和 FFmpeg
└─ 最终 MP4 / MP3
```

桌面窗口与网页使用相同账号和业务页面。浏览器不能直接读取任意本机文件，因此网页上的文件选择、试听和渲染由当前电脑安装的 StoryForge 本机制作服务执行；完整桌面窗口无需一直打开。

## 数据边界

首次启动时选择一个本地固定磁盘目录作为 `<StoryForgeData>`。它保存设置、数据库/连接状态、日志、缓存、渲染工作区、组件、更新状态和备份元数据。

程序目录、数据目录、视频素材、音乐和最终输出可以位于不同磁盘。正式更新包不得包含、覆盖或删除 `StoryForgeData`。

GitHub 私有仓库只保存源码、文档、构建脚本和正式发布附件，不保存小说正文、账号密码、API Key、生产数据库、素材或成品。

## 源码运行

要求：Windows 10/11 x64、Python 3.11 或 3.12、Microsoft Edge WebView2 Runtime。

```powershell
py -3.12 -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r '.\requirements.txt'
& '.\.venv\Scripts\python.exe' '.\run.py'
```

本地 Kokoro 开发环境：

```powershell
& '.\.venv\Scripts\python.exe' -m pip install -r '.\requirements-ai.txt'
```

## 测试

```powershell
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -p 'test_*.py'
node --check '.\ui\app.js'
git diff --check
```

快速与正式发布门禁见 [发布与回归门禁](docs/RELEASE_GATES.md)。构建成功不等于正式包可交付；正式包还必须通过冻结程序启动、内置 FFmpeg、本地 TTS、真实长渲染、包体哈希和独立解压冒烟。

## Windows 正式打包

正式员工包使用完整 onedir 目录，不能只复制 EXE，也不能在 ZIP 内直接运行。

```powershell
& '.\scripts\build_exe.ps1' `
  -ReleaseBuild `
  -RequireStableAcceptance `
  -WithLocalAI `
  -StableStressSeconds 600 `
  -OutputDirectory 'D:\StoryForgeRelease\1.0.0\dist' `
  -WorkDirectory 'D:\StoryForgeRelease\1.0.0\work' `
  -HubEndpoint 'http://<Hub-IP>:8765'
```

随后使用 `scripts/build_update_package.py` 从已验收目录生成统一 ZIP。该 ZIP 既可用于新安装，也可供管理员手动发给员工更新。仅把 ZIP 上传 GitHub Release 不会自动下发到员工端；只有管理员在正在运行的 Hub 内主动发布更新清单时，客户端才会发现该版本。

## 当前权限模型

- 管理员：管理小说、平台、口令、成员、设备、全部记录、更新和备份。
- 员工：使用已有小说/平台/口令，配置自己的制作方案，试听配音，提交、查看、重试和归档自己的任务。
- 员工不能修改或删除团队固定资料，也不能把自己的电脑设为 Hub 主机。

权限和 RPC 的唯一代码合同位于 `storyforge/rpc_contract.py`，不要在前端、Hub 和 Worker 分别维护另一套白名单。

## 输出目录

```text
<员工选择的输出目录>\待发布\
└─ <平台>_<口令>_<小说>_B<批次短ID>\
   ├─ 001_<平台>_<口令>_E001-E002_V01_<批次>.mp4
   ├─ 002_<平台>_<口令>_E001-E002_V02_<批次>.mp4
   └─ ...
```

`audio_only` 批次只包含一份 `.mp3`。调试文件留在 `StoryForgeData`，发布目录保持简洁。

## 明确不包含

- 自动登录或自动上传 TikTok。
- TikTok 平台合规检查。
- 多任务重型并行渲染；每台员工电脑默认串行处理一个重型任务，以稳定优先。
- 把员工素材或最终视频集中传回 Hub。
- 把用户数据上传到 GitHub。

## 维护原则

1. `storyforge.__version__` 是唯一应用版本来源。
2. 数据库 Schema、Settings Schema、预设 Schema 和 RPC 协议分别迁移，不以应用版本代替。
3. 渲染成功后再原子发布成品并记录素材使用；失败不得留下半成品。
4. 核心程序和语言组件分别更新，均校验 SHA-256 并保留回滚。
5. 任何接手或嫁接工作先阅读 [项目接手与功能嫁接说明](docs/STORYFORGE_INTEGRATION_REPORT.md)。
