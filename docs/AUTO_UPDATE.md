# StoryForge 局域网软件更新

> 适用源码版本：StoryForge Studio `0.4.4`。当前交付边界与发布验收见 [0.4.4 稳定版交付报告](V0.4.4_STABLE_RELEASE.md)；0.4.2 → 0.4.3 的一次性受控迁移规则仍保留在下文。

0.4.4 员工版采用单目录便携安装，推荐目录为 `D:\StoryForge`，运行数据统一位于 `D:\StoryForge\StoryForgeData`。Hub 可以为**已经运行 0.4.3 或更高版本**的制作电脑发布完整 ZIP 快照；客户端默认每 60 秒轮询，发现更高版本后在空闲时下载、校验，并在安全重启时安装。它不是运行中热更新，正在制作的任务不会被覆盖或打断。

## 0.4.2 → 0.4.3：禁止自动推送

0.4.2 的旧更新器不满足本次便携目录迁移的安全条件，因此 0.4.3 **不得通过 0.4.2 的 Hub 自动更新发布**。这是一次性发布规则，不是建议项。

1. 管理员先撤回或停止面向 0.4.2 员工机的自动更新公告。
2. 在员工电脑确认没有正在配音、转码或渲染的任务，完全退出旧桌面端和旧 Worker。
3. 把经过验收的完整 0.4.3 目录解压到纯英文、可写的本地固定磁盘目录，推荐 `D:\StoryForge`。不能覆盖式拼接旧目录，也不能只复制 EXE。
4. 首次启动 0.4.3 并用原账号密码登录。程序会把旧 C 盘中的长期数据复制到 `StoryForgeData`，目标中已有的文件绝不被旧数据覆盖；检测到旧 Worker 仍运行时会停止迁移，防止两个队列并发。
5. 确认版本上报为 0.4.3，并在该员工机完成一次真实 MP4 + MP3 任务。验收前保留旧 C 盘数据用于诊断和回滚。

成功迁移后，旧设置、数据库、日志、命令、Manifest 和未知文件仍保留在旧目录；程序只会清理确定可重新生成的旧缓存、临时文件、旧更新下载和渲染媒体，不会删除长期资料。不要在验收前手工清空 `%APPDATA%\StoryForgeStudio` 或 `%LOCALAPPDATA%\StoryForgeStudio`。

## 安全与执行边界

- 当前版本只读取 `storyforge.__version__`，`pyproject.toml` 也动态使用该字段。
- 更新清单由制作电脑自动登记后获得的设备凭据做 HMAC-SHA256 校验，更新包再按清单中的大小和 SHA-256 校验。该凭据由账号密码接入流程后台签发并安全保存，不需要人工生成或复制。
- ZIP 发布和安装前都会拒绝绝对路径、`..` 路径、重复路径、符号链接、缺失启动文件、版本不一致、过多文件和异常解压体积。
- 下载先写 `.part` 文件，全部校验通过才原子移动到更新缓存。
- 自动下载、安装临时文件、状态和最多 3 日的回滚副本都位于 `D:\StoryForge\StoryForgeData\updates`，不会写回旧 C 盘 StoryForge 目录，也不会直接覆盖正在运行的软件。
- 安装仅在用户明确安排后发生；若桌面制作端队列仍处于配音、合成或完整视频渲染阶段，本次退出不启动更新器，保留待安装状态。
- 安装器在 StoryForge 进程退出后运行，覆盖前备份同名旧文件；复制失败会恢复已覆盖文件。`StoryForgeData` 是受保护目录：更新包不得包含它，安装器不得覆盖或删除它。
- Hub 主机不会自动消费自己发布的更新。发布前应先备份并用完整目录人工升级主机。
- 安装 Worker 不会自动提权；程序目录必须对当前 Windows 用户可写，不建议放入 `Program Files`。
- 当前 Hub 默认是局域网 HTTP。HMAC 和摘要可检测篡改，但不替代安全传输；跨公网部署应使用 VPN 或 HTTPS 网关。

## 制作更新包

先按正常流程构建一个完整的软件文件夹，然后运行：

```powershell
python scripts\build_update_package.py "<已验收的完整发布目录>" `
  --entrypoint "StoryForge Studio.exe" `
  --version <SemVer> `
  --output "<更新包输出路径>"
```

脚本会在指定输出路径生成 ZIP 和同名 `.manifest.json` Sidecar。尖括号内容是发布人员必须替换的占位符，不是可直接复制执行的最终命令。

脚本只生成更新包，不会发布，也不会修改任何客户端。ZIP 根目录包含 `storyforge-update.json`，格式如下：

```json
{
  "schema_version": 1,
  "version": "x.y.z",
  "entrypoint": "StoryForge Studio.exe"
}
```

ZIP 根目录还应包含 `StoryForge Studio.exe` 和该发布版需要的 `local-ai\kokoro` 等完整安装内容；它是程序目录快照，不是二进制差分。构建来源中不得带有任何员工运行产生的 `StoryForgeData`，ZIP 内也不得出现该目录。主机随后通过软件界面选择 ZIP、填写同一版本号和更新说明，并调用 `publish_update`。Hub 只会发布通过结构、版本和 SHA-256 检查的包。Sidecar `*.manifest.json` 用于构建审计；Hub 会对实际复制后的 ZIP 重新计算权威 Manifest。

## 设置字段

`settings.hub` 新增：

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `auto_update_enabled` | `true` | 客户端运行时后台检查 |
| `auto_download_updates` | `true` | 发现新版后只自动下载并校验，不自动安装 |
| `update_check_minutes` | `1` | 检查间隔，允许 1–1440 分钟 |

## 桌面 API

| 方法 | 使用方 | 作用 |
|---|---|---|
| `get_update_status()` | 全部 | 读取当前版本、可用版本、下载和重启状态 |
| `check_for_updates()` | 客户端 | 立即检查；开启自动下载时同时完成安全下载 |
| `download_update()` | 客户端 | 手动下载当前新版本 |
| `schedule_update_on_restart()` | 客户端 | 明确安排在安全退出后安装并重新打开 |
| `cancel_scheduled_update()` | 客户端 | 取消安装安排，保留已下载文件 |
| `publish_update(package_path, version, release_notes)` | Hub 主机 | 发布一个预构建 ZIP |
| `clear_published_update()` | Hub 主机 | 停止向客户端公告当前版本；不删除已验证 ZIP |

`get_bootstrap().data.update_status` 与 `get_update_status().data` 使用同一结构。常见 `state`：

- 客户端：`checking`、`up_to_date`、`available`、`downloading`、`downloaded`、`scheduled`、`deferred`、`applying_on_restart`、`error`
- 主机：`publisher_idle`、`published`

关键字段包括 `current_version`、`available_version`、`message`、`checked_at`、`package_path`、`downloaded`、`apply_on_restart`、`restart_required`、`rendering_busy`、`release_notes`、`published_at`、`error`。主机另外返回 `published_update`。

## Hub HTTP 合同

客户端使用账号密码登记后自动保存的 Bearer 设备凭据调用；下列 Token 是内部协议字段，不向日常使用者展示：

- `GET /updates/manifest`：返回经过当前设备令牌 HMAC-SHA256 签名的清单或 `null`。
- `GET /updates/package?version=x.y.z`：只允许下载当前公告版本，响应同时返回 `Content-Length` 和 `X-Content-SHA256`。

两个路由都要求令牌对应一个仍启用的软件账号。普通远程制作客户端不能通过设备 Hub RPC 发布或替换软件包；发布入口只开放在 Hub 主机桌面界面，以及经登录、CSRF 校验、受控 ZIP 上传和 `hub.manage` 权限保护的 Hub 网页管理界面。

### 老版本员工端没有“设置”入口时

本节只用于旧版的一般修复或取得与旧版兼容的包，**不适用于 0.4.2 → 0.4.3**；该跨版本仍必须按本文开头使用完整包受控迁移。

0.4.0-rc5 起员工会保留“设置 → 本机维护与软件更新”，无需管理员代操作。更早的客户端可能完全隐藏设置；不要为升级临时授予管理员权限，直接使用 Hub 提供的密码会话下载通道：

1. 在制作电脑浏览器打开 Hub 网页并使用员工账号、密码登录。
2. `GET /web/api/update` 读取当前公告版本；返回的 `download_url` 是本次发布包的下载地址。
3. `GET /web/api/update/package`，或清单给出的 `GET /web/api/update/package?version=x.y.z`，下载当前已发布 ZIP。

这两个网页路由接受普通员工的已登录 Cookie，不要求 `hub.manage`，也不向浏览器暴露设备 Bearer 凭据、主机文件路径或发布/撤回能力。包响应带 `Content-Length`、`X-Content-SHA256`、`ETag` 和断点续传头；匿名访问返回 `401`，非当前公告版本返回 `404`。浏览器只能安全下载，不能覆盖仍在运行的 EXE；取回包后仍须关闭旧版 StoryForge，再解压到可写的新目录或按交付说明替换旧目录。

## 0.4.3 生产与本机制作服务说明

软件更新协议与生产审批无关。0.4.3 的制作电脑在浏览器即时预览后，直接创建 `job_kind="full"`、`status="queued"` 的完整任务，`preview_required=false`；不会为新批次生成或审批独立 preview。操作可从桌面窗口、Hub 网页或该电脑的本地制作网页发起；浏览器中的媒体调用由当前电脑的本机制作服务执行。更新后应确认 `StoryForge Local Worker` 登录任务仍存在并已随新版程序重启，完整窗口无需常开。

封面结尾可开启或关闭；关闭只移除封面和封面动画，CTA 旁白、字幕和顶部搜索口令继续。Settings Schema 19 提供三种模式：默认 `video_and_mp3` 同时交付完整 MP4 和同基名 48 kHz、192 kbps 纯旁白 MP3；`audio_only` 对所选合并正文只交付一份 MP3；`reuse_audio` 使用 StoryForge MP3 内嵌的小说、口令和字幕时间索引更换视频素材。旧 `export_narration_audio=true`、`false` 或缺失均迁移为默认模式。最终 MP4/MP3 只进入员工发布批次目录，WAV、ASS、日志和其他技术产物保留在本机 `StoryForgeData\render-work`，旁白默认不共享到 Hub。更新后建议在一台制作电脑分别完成三种模式的最小任务验收，并检查网页连接的本机 FFmpeg/TTS 状态和封面结尾开关。

## 日志、清理与卸载

- 桌面启动与运行日志：`D:\StoryForge\StoryForgeData\logs`。
- 单任务诊断：`D:\StoryForge\StoryForgeData\render-work\<批次>\<任务>\job-error.log` 或 `render-error.log`。
- 更新缓存与回滚状态：`D:\StoryForge\StoryForgeData\updates`。
- 0.4.3 便携版退出程序和 Worker 后，删除 `D:\StoryForge` 即可卸载程序及其运行数据；无需再到 C 盘查找新版数据。员工另行选择的素材、音乐和输出目录不在卸载范围内。
- 从 0.4.2 迁移的旧 C 盘数据须在 0.4.3 验收通过后由管理员决定归档或删除。程序不会自动删除仍有诊断价值的旧数据库、设置、日志、Manifest 或未知文件。
