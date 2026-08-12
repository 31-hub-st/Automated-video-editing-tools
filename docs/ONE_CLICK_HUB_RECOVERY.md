# StoryForge Hub 新电脑一键恢复

这条入口仅限全新 Hub 电脑。恢复会把 GitHub 私有快照作为唯一权威资料整体还原，不会合并、修改或接管已有数据库。

## 普通用户只做五步

1. 在浏览器登录有权读取私有仓库的 GitHub 账号，下载仓库 ZIP。
2. 解压 ZIP 到新电脑的普通文件夹，不要在 ZIP 内直接运行。
3. 双击解压后根目录的 `一键恢复StoryForge-Hub.cmd`。
4. 按中文提示允许管理员权限并完成浏览器授权。
5. 等待恢复成功，窗口显示成功后会尝试自动打开 Hub 网页。

## 使用前

1. 在旧 Hub 上先按 [新电脑恢复说明](NEW_MACHINE_RECOVERY.md) 发布一份最新 `hub-state-latest` 快照。仅下载最新程序不能恢复业务数据；小说、账号、口令与制作记录来自这个私有快照。
2. 新电脑准备本地 `D:` 盘，并把本私有仓库完整放到新电脑普通文件夹中；不要在 ZIP 内直接运行。
3. 确认整个 `D:\StoryForgeHub` 不存在或只包含一个空的 `Data` 文件夹，系统中没有 `StoryForge Hub` 计划任务、8765 监听端口或正在运行的 StoryForge 正式进程。

## 一键执行

双击仓库根目录：

```text
一键恢复StoryForge-Hub.cmd
```

随后：

- 在 Windows UAC 窗口中允许管理员权限；
- 若未安装 GitHub CLI，脚本会通过 `winget` 安装；
- 浏览器打开后，登录有权读取 `31-hub-st/Automated-video-editing-tools` 私有仓库的 GitHub 账号；
- 等待正式 Release 下载、全部摘要校验、Hub 快照恢复和部署验证完成。

脚本固定使用：

- 安装目录：`D:\StoryForgeHub`
- 正式 DataRoot：`D:\StoryForgeHub\Data`
- Hub 端口：`8765`

任一全新主机预检不通过都会中文拒绝，不会借助 `-ReplaceExistingData` 覆盖旧资料。该参数只允许部署脚本把已经校验的快照恢复到本次流程刚初始化的空白 DataRoot。

## 完成后

- 脚本会显示本机和局域网网页地址并尝试打开网页。
- 若希望员工电脑完全不用改地址，请让新 Hub 继续使用原固定 IP `10.0.0.225`；否则请在路由器为新地址设置 DHCP 地址保留，并同步修改员工端地址。
- Windows DPAPI 保护的旧 API Key 不能跨电脑解密；请在新 Hub 中重新填写。以后电脑重启后，要登录本次安装使用的 Windows 管理员账号。
- 日志保存在 `%LOCALAPPDATA%\StoryForge\RecoveryLogs`。失败窗口不会立即关闭，请记录中文原因和日志路径。

需要自定义路径、端口或人工审核每一步时，不要使用一键入口，改按 [新电脑恢复说明](NEW_MACHINE_RECOVERY.md) 执行受支持的手动命令。
