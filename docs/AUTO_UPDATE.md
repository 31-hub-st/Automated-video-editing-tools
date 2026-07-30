# StoryForge 局域网软件更新

> 适用源码版本：StoryForge Studio `0.4.0-rc7`。候选版必须先完成 Hub 主机和全部员工制作电脑验收；最终构建目录、更新包文件名和 SHA-256 在正式构建验收后写入交付清单，本文不预填。

StoryForge `0.2.0` 起，Hub 主机可以发布一个已构建的 ZIP 软件包；连接该 Hub 的制作电脑默认每 60 秒轮询一次，发现更高版本后自动下载到本机缓存。它不是推送或热更新。下载不会覆盖正在运行的程序，安装必须由使用者明确选择“下次重启安装”。

## 安全与执行边界

- 当前版本只读取 `storyforge.__version__`，`pyproject.toml` 也动态使用该字段。
- 更新清单由制作电脑自动登记后获得的设备凭据做 HMAC-SHA256 校验，更新包再按清单中的大小和 SHA-256 校验。该凭据由账号密码接入流程后台签发并安全保存，不需要人工生成或复制。
- ZIP 发布和安装前都会拒绝绝对路径、`..` 路径、重复路径、符号链接、缺失启动文件、版本不一致、过多文件和异常解压体积。
- 下载先写 `.part` 文件，全部校验通过才原子移动到更新缓存。
- 自动下载只写 `%APPDATA%\StoryForgeStudio\updates\downloads`，不会写软件安装目录。
- 安装仅在用户明确安排后发生；若桌面制作端队列仍处于配音、合成或完整视频渲染阶段，本次退出不启动更新器，保留待安装状态。
- 安装器在 StoryForge 进程退出后运行，覆盖前备份同名旧文件；复制失败会恢复已覆盖文件。更新包不会删除安装目录中未包含的文件。
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

ZIP 根目录还应包含 `StoryForge Studio.exe` 和该发布版需要的 `local-ai\kokoro` 等完整安装内容；它是完整目录快照，不是二进制差分。主机随后通过软件界面选择 ZIP、填写同一版本号和更新说明，并调用 `publish_update`。Hub 只会发布通过结构、版本和 SHA-256 检查的包。Sidecar `*.manifest.json` 用于构建审计；Hub 会对实际复制后的 ZIP 重新计算权威 Manifest。

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

0.4.0-rc5 起员工会保留“设置 → 本机维护与软件更新”，无需管理员代操作。更早的客户端可能完全隐藏设置；不要为升级临时授予管理员权限，直接使用 Hub 提供的密码会话下载通道：

1. 在制作电脑浏览器打开 Hub 网页并使用员工账号、密码登录。
2. `GET /web/api/update` 读取当前公告版本；返回的 `download_url` 是本次发布包的下载地址。
3. `GET /web/api/update/package`，或清单给出的 `GET /web/api/update/package?version=x.y.z`，下载当前已发布 ZIP。

这两个网页路由接受普通员工的已登录 Cookie，不要求 `hub.manage`，也不向浏览器暴露设备 Bearer 凭据、主机文件路径或发布/撤回能力。包响应带 `Content-Length`、`X-Content-SHA256`、`ETag` 和断点续传头；匿名访问返回 `401`，非当前公告版本返回 `404`。浏览器只能安全下载，不能覆盖仍在运行的 EXE；取回包后仍须关闭旧版 StoryForge，再解压到可写的新目录或按交付说明替换旧目录。

## 0.3.4 生产与本机制作服务说明

软件更新协议与生产审批无关。0.3.4 的制作电脑在浏览器即时预览后，直接创建 `job_kind="full"`、`status="queued"` 的完整任务，`preview_required=false`；不会为新批次生成或审批独立 preview。操作可从桌面窗口、Hub 网页或该电脑的本地制作网页发起；浏览器中的媒体调用由当前电脑的本机制作服务执行。更新后应确认 `StoryForge Local Worker` 登录任务仍存在并已随新版程序重启，完整窗口无需常开。

封面结尾可开启或关闭；关闭只移除封面和封面动画，CTA 旁白、字幕和顶部搜索口令继续。Settings Schema 19 提供三种模式：默认 `video_and_mp3` 同时交付完整 MP4 和同基名 48 kHz、192 kbps 纯旁白 MP3；`audio_only` 对所选合并正文只交付一份 MP3；`reuse_audio` 使用 StoryForge MP3 内嵌的小说、口令和字幕时间索引更换视频素材。旧 `export_narration_audio=true`、`false` 或缺失均迁移为默认模式。最终 MP4/MP3 只进入员工发布批次目录，WAV、ASS、日志和其他技术产物保留在本机 `render-work`，旁白默认不共享到 Hub。更新后建议在一台制作电脑分别完成三种模式的最小任务验收，并检查网页连接的本机 FFmpeg/TTS 状态和封面结尾开关。
