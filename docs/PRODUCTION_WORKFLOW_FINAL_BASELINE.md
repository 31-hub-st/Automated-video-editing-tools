# StoryForge 制作流程最终需求与接口基线

更新日期：2026-08-12

适用版本：StoryForge Studio `v1.0.2`

用途：后续版本验收、迁移或嫁接到其他程序时，以本文件作为制作流程的当前基线。

## 1. 产品目标

把固定小说资料转换为适合 TikTok 的 9:16 英语小说推广视频。Hub 负责小说、平台、口令、账号、AI 文案与生产记录；每台员工电脑使用自己的视频素材、音乐、配音运行时、FFmpeg/GPU、临时目录和输出目录。

核心质量目标：故事和字幕正确、配音自然且有吸引力、每条成片画面组合不同、口令始终正确、员工可连续建立批次，不把渲染压力集中到 Hub。

## 2. 一页制作流程

1. 选择小说。
2. 选择平台、口令、发布账号及要合并的正文分集。
3. 选择制作方式、女声/已有 MP3、语速、视频速度、字幕、卡片、音乐和本机目录。
4. 右侧即时预览开头、正文字幕和封面结尾；不生成审核样片。
5. 点击生成后立即建立下一批；已提交任务使用冻结配置继续排队执行。

制作页采用渐进展开，只显示当前制作方式需要的字段。平台、口令、发布账号、分集和本机文件夹不由团队预设覆盖。

## 3. 三种制作方式

### 3.1 常规视频生成

- 接口值：`output_mode=video_and_mp3`
- 输入：所选正文、真实女声、视频素材；BGM 可自动、手动或关闭。
- 输出：每个任务只交付一份最终 MP4；不会额外生成配音 MP3。

### 3.2 仅生成配音

- 接口值：`output_mode=audio_only`
- 所选分集合并成一段连续正文。
- 每批固定只创建一个任务、输出一份 MP3，不按“生成总视频数”重复 TTS。
- 不要求视频或音乐文件夹，不执行视频渲染。

### 3.3 已有配音更换素材

- 接口值：`output_mode=reuse_audio`
- 输入字段：`source_narration_audio`，只接受 MP3。
- 不重新调用文案和 TTS；使用新的本机视频素材、字幕样式和 BGM 生成新 MP4。
- 新版 StoryForge MP3 内嵌 ID3 `TXXX/StoryForgeNarration` 索引，包含小说 ID、分集、口令、润色正文、时长和精确字幕 cue；复制到另一台员工电脑后仍可安全复用。
- 同一台电脑上的旧 MP3 可继续读取私有索引。既无内嵌索引、原电脑也无私有索引时必须阻止复用，不能猜测字幕或口令。

## 4. 正文与分集

- 一个批次只对应一部小说和一个平台。
- 员工可选择一个或多个分集；系统按正文顺序合成一个连续内容单元。
- 所选分集之间不插入上集回顾。
- 从第 1 集开始时无回顾；整组选集从后续集开始时，只在整组最前面生成一次回顾。
- 预计超过 10 分钟只提醒，不自动拆分、不阻止提交。
- `target_video_count` 表示同一连续内容要生成多少个不同素材版本，不设业务上限；仅生成配音模式例外，固定为 1。
- 重复完全相同配置只提示，不禁止提交。

## 5. 配音与语速

- 默认美式英语女声；同一批始终使用一个女声，下一批可更换。
- 新批次默认沿用该小说最近一次成功任务的音色，但不是永久锁定。
- 语速档位：舒适 220、推荐 240、快速 260、极快 280 WPM。
- 自定义范围：200–280 WPM。
- 切换语速后自动生成并播放 8–12 秒真实试听；按小说文本、服务、声音和 WPM 缓存。无法真实生成时明确报错，不使用浏览器模拟声音冒充。
- Kokoro、Edge 和 Deepgram 均覆盖上述范围；Deepgram 请求参数最多使用 1.5，超过部分在本机通过 FFmpeg `atempo` 无变调补足。
- BGM 默认音量 28%，旁白出现时自动压低；模式为 `auto / manual / none`。

## 6. 字幕、口令和画面

- 字幕保持 TikTok 安全区、自动居中与防越界。
- 支持整句/语义停顿切分和两种逐词模式：
  - `cumulative`：词语随旁白逐步出现，当前词变色/弹出；
  - `single`：画面只显示当前朗读单词。
- `subtitle_word_mode=off` 时使用普通字幕。
- 字幕模式和样式默认沿用员工上次选择。
- 简介卡和顶部口令卡都有独立开关、绝对开始时间和显示时长；绝对时间从成片第 `0` 秒起算，口令卡时长为 `0` 时持续到视频结尾。关闭任一卡片不影响另一张卡片、旁白或字幕；口令值允许字母、数字及混合内容。
- 平台搜索文案和结尾引导文案默认从管理员维护的平台模板解析；员工可为单个批次填写覆盖文案，覆盖内容随任务冻结，不修改团队平台模板。
- 素材原声始终删除；非 9:16 素材使用模糊背景保留完整画面。
- 封面结尾可启用或关闭；启用时封面动画铺满全屏，CTA 由旁白和字幕继续展示，不再使用简陋平台结尾卡。

## 7. 视频素材、速度与去重

- 产品术语统一为“视频素材”。
- 固定播放速度预设：1.0、1.1、1.25、1.4、1.5×；自定义范围 0.8–3.0×。
- 同一批全部素材使用员工选择的固定速度；不做智能速度判断，也不私自轻微变速。
- 一个素材足够长时优先使用单个长素材；不足时自动拼接多个素材。
- 拼接时先使用每个不同素材，再考虑复用；转场为 `cut` 或 `fade`，默认 `cut`，`fade` 固定 0.2 秒。
- 视频素材不按小说题材匹配。系统只递归扫描员工本批选择的视频素材文件夹；所选目录没有可解码视频时才失败。
- 每个成片通过素材选择、起始位置、镜像和裁切等方式形成不同画面组合。去重目标是“每条最终视频不同”，不是永久禁止某部小说再次使用某个素材。
- 不进行渲染后的感知相似度扫描，以免再次增加生产耗时。

## 8. 输出与存储

- 视频：H.264、1080×1920、默认 60 FPS。
- 员工发布目录按模式只出现最终成品：常规视频生成只放 MP4，仅生成配音只放一份 MP3，已有配音更换素材只放新 MP4。
- 文件按批次和稳定短名称排序，便于员工一次选择多个视频批量发布。
- Manifest、ASS、内部 WAV、原稿、渲染命令、恢复日志和缓存只放在员工选择的 `<StoryForgeData>`，不混入发布目录。
- Hub 只保存生产状态、业务记录、校验和本机引用，不上传员工成片、MP3、素材或中间文件。
- 小说库只累计 `successful_video_count` 和 `last_production_at`；口令不展示或累计使用次数。

## 9. 多电脑与权限

- 角色只有管理员和员工。
- 管理员维护小说、平台、口令、成员、Hub、团队预设和全部记录。
- 员工可制作、试听、调整本批设置、管理本机目录、查看/重试自己的任务和更新自己的电脑。员工可为自己的单个批次覆盖平台搜索文案和结尾引导文案，但不能修改小说、团队平台模板和历史口令，也不因此获得 `platforms.manage`；团队模板仍由管理员管理。
- 员工只用 8 位账号密码登录；新员工默认密码 `xs123456`。人工操作中不显示或要求令牌。
- 每台员工电脑安装 StoryForge 一次。桌面和该电脑上的网页入口使用相同功能；网页任务仍由当前员工电脑的本机制作服务执行。
- 员工电脑路径在发送 Hub 前替换为 `worker://` 引用，Hub 不保存其他电脑的绝对路径。
- 核心程序使用自动更新；语言组件使用独立的安全更新框架，安装到 `<StoryForgeData>\components`，支持逐文件哈希校验、原子切换和上一版回退。当前测试完整包仍内置日语链路。
- Hub 固定资料备份保留 72 小时且最多 3 份，相同内容按摘要去重；备份不包含员工视频素材、最终成品或渲染缓存。

## 10. 预设与偏好

- 一套方案可保存配音节奏、字幕、卡片、画面、BGM 模式、视频速度、转场和输出设置。
- 团队默认和平台模板由管理员在设置中维护，只影响之后新建的批次；制作台仍可覆盖本批参数，但本批平台文案覆盖不回写团队平台模板。
- 员工可创建、修改和删除自己的个人方案；管理员可查看和删除全部成员方案。旧版内置、团队共享和无所有者方案均不再显示。
- 制作方式、口令、音色、语速、字幕模式、视频速度、转场、BGM 和本机目录默认沿用员工/小说/平台对应的上次选择；分集每批重新确认。

## 11. 关键接口字段

```text
production_settings.output_mode
production_settings.narration_wpm
production_settings.video_playback_speed
production_settings.video_transition
production_settings.subtitle_word_mode
production_settings.bgm_mode
production_settings.bgm_file
production_settings.intro_card_enabled
production_settings.intro_card_start_seconds
production_settings.intro_card_duration_seconds
production_settings.code_card_enabled
production_settings.code_card_start_seconds
production_settings.code_card_duration_seconds
production_settings.cover_outro_enabled
platform_search_text
platform_ending_text
source_narration_audio
target_video_count
episode_ids
platform_id
promo_code_id
publishing_account_id
video_folder
music_folder
output_folder
```

范围与枚举：

```text
output_mode = video_and_mp3 | audio_only | reuse_audio
narration_wpm = integer 200..280
video_playback_speed = number 0.8..3.0
video_transition = cut | fade
subtitle_word_mode = off | cumulative | single
bgm_mode = auto | manual | none
intro_card_enabled = true | false
intro_card_start_seconds = number >= 0
intro_card_duration_seconds = number 2.5..8.0
code_card_enabled = true | false
code_card_start_seconds = number >= 0
code_card_duration_seconds = number >= 0（0 表示持续到视频结尾）
```

`video_and_mp3` 是为兼容既有接口保留的枚举名，当前语义为“常规视频生成且只交付 MP4”，不得根据名称推断还会输出 MP3。

所有提交任务必须冻结上述制作设置。已经排队的任务不得被下一批界面修改。

旧草稿和已冻结作业缺少新增卡片字段时，使用兼容默认值继续读取和执行；兼容读取不得回写旧作业。`platform_search_text` 与 `platform_ending_text` 为空时使用团队平台模板，非空时只作为本批覆盖。

## 12. 当前验收结论

本节记录既有基线证据；本次新增卡片时间与本批平台文案合同仍须以 `v1.0.2` 的完整测试、构建和发布门结果为准，本文件本身不代表版本已经构建或发布。

- 编译检查通过。
- 媒体、字幕、配音、生产管线、资料库、预设、UI、Hub、员工本机 Worker 与网页协同测试通过。
- 真实 FFmpeg 冒烟测试确认：内嵌索引后的 MP3 可正常解码，索引在文件复制后可读取。
- 无索引 MP3 的换素材流程会主动失败，不会生成字幕或口令可能错误的成片。
