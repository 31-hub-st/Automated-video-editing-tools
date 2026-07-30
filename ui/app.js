(() => {
  "use strict";

  const DEFAULT_PREVIEW_SECONDS = 15;
  const MERGED_DURATION_WARNING_SECONDS = 10 * 60;
  const RECORD_POLL_INTERVAL_MS = 3000;

  const state = {
    bootstrapped: false,
    settings: null,
    platforms: [],
    jobs: [],
    archivedJobs: [],
    archivedJobsTotal: 0,
    archivedJobsLoaded: false,
    archivedJobsLoading: false,
    archivedJobsPageSize: 50,
    system: {},
    selectedPlatformId: "",
    platformLogoPreviewSource: "",
    pollTimer: null,
    pollEnabled: false,
    pollInFlight: false,
    pollEpoch: 0,
    pollFailureCount: 0,
    jobVisualSignature: "",
    queueVisualSignature: "",
    recordPollTimer: null,
    recordPollInFlight: false,
    recordPollPending: false,
    recordPollUrgent: false,
    recordPollLastAt: 0,
    previousJobStates: new Map(),
    novels: [],
    publishingAccounts: [],
    productionRecords: [],
    productionRecordGroups: null,
    selectedRecordIds: new Set(),
    softwareUsers: [],
    selectedSoftwareUserId: "",
    hubRuntimeStatus: null,
    updateStatus: null,
    browserUpdateDownloadUrl: "",
    stylePreviewScene: "intro",
    styleEditingScope: "global",
    customStylePresets: [],
    creatingSoftwareUser: false,
    managedDevices: [],
    managedDevicesLoading: false,
    managedDevicesError: "",
    managedDevicesRequestId: 0,
    managedDeviceSelection: new Set(),
    managedDeviceConfigs: [],
    managedDeviceConfigDetails: new Map(),
    managedDeviceFleetTimer: null,
    runtimeRefreshTimer: null,
    managedDevicesRefreshedAt: "",
    deviceSyncStatus: null,
    selectedNovelId: "",
    selectedNovel: null,
    productionNovelId: "",
    productionNovel: null,
    selectedPublishingAccountId: "",
    publishingPlatformFilter: "",
    libraryLayout: "cards",
    libraryQuery: "",
    libraryPlatformFilter: "",
    libraryLanguageFilter: "",
    librarySelectionMode: "",
    recordStatusFilter: "",
    recordNovelFilter: "",
    recordBatchFilter: "",
    recordMemberFilter: "",
    recordDeviceFilter: "",
    recordDateFrom: "",
    recordDateTo: "",
    recordTrashFilter: false,
    jobArchiveView: "active",
    jobBatchDisclosure: new Map(),
    productionLocalTab: "create",
    productionPreviewDrawerOpen: false,
    productionSectionExpanded: {
      content: true,
      voice: false,
      visual: true,
      output: false,
    },
    productionPreviewScene: "intro",
    productionPresets: [],
    selectedProductionPresetId: "",
    lastQueuedBatch: null,
    importSource: "paste",
    importFilePath: "",
    importTargetNovelId: "",
    libraryBackendReady: true,
    libraryBackendError: "",
    detailReturnFocus: null,
    recordArtifactReturnFocus: null,
    previewTimers: new Map(),
    lastDetailJobSignature: "",
    webSession: null,
    webCapabilities: {},
    webUploadAssets: new Map(),
    webDefaultFolders: {},
    localWorker: null,
    localWorkerIssue: null,
    localWorkerSelfCheck: null,
    queueConnection: { state: "connected", reconnecting: false, retry_in_seconds: 0, message: "" },
    wpmPreviewAudio: null,
    wpmPreviewStopTimer: null,
    wpmPreviewDebounceTimer: null,
    wpmPreviewRequestId: 0,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  function hasDesktopBridge() {
    const api = window.pywebview?.api;
    return Boolean(
      api
      && (
        typeof api.desktop_session_status === "function"
        || typeof api.desktop_rpc === "function"
      )
    );
  }
  const softwareRoleCatalog = {
    admin: {
      label: "管理员",
      summary: "管理团队资料、成员账号、全部生产记录和多电脑协同。",
    },
    producer: {
      label: "员工",
      summary: "制作、查看和重试自己的任务；不能修改团队资料或系统设置。",
    },
  };
  const softwarePermissionCatalog = [
    ["library.edit", "编辑小说内容", "导入或修改正文、简介和封面", "内容与平台"],
    ["platforms.manage", "调整平台资料", "修改推广平台和小说绑定", "内容与平台"],
    ["promo_codes.manage", "管理推广口令", "录入、启用或停用历史口令", "内容与平台"],
    ["publishing_accounts.manage", "管理发布账号", "维护人工发布使用的账号库", "内容与平台"],
    ["drafts.manage_all", "管理团队批次", "查看和调整其他成员的制作批次", "生产协作"],
    ["records.view_all", "查看团队记录", "查看所有成员的制作记录和成片", "生产协作"],
    ["jobs.retry_all", "重试团队任务", "处理其他成员失败或中断的任务", "生产协作"],
    ["records.export", "导出生产记录", "导出团队后台数据", "生产协作"],
    ["updates.manage_own", "更新自己的电脑", "检查、下载并安装当前制作电脑的软件更新", "本机维护"],
    ["users.manage", "管理成员账号", "新建、停用账号或更换账号类型", "账号与主机"],
    ["permissions.manage", "修改高级权限", "为单个账号设置特殊权限", "账号与主机"],
    ["hub.manage", "管理多电脑协同", "配置主电脑、真实设备和统一制作设置", "账号与主机"],
  ];
  const storyMoodCatalog = {
    suspense: { label: "悬念", voice: "戏剧张力", folder: "悬疑" },
    romance: { label: "浪漫", voice: "温暖亲密", folder: "浪漫" },
    sad: { label: "悲伤", voice: "冷静克制", folder: "悲伤" },
    revenge: { label: "复仇 / 爽文", voice: "清晰强势", folder: "爽文" },
  };
  const visualStylePresetCatalog = {
    intro: {
      editorial_white: { label: "杂志白卡", values: { "intro-font": "Arial", "intro-headline-size": 66, "intro-headline-color": "#FFE06A", "intro-body-size": 32, "intro-body-color": "#263247", "intro-label-size": 24, "intro-label-color": "#315BD8", "intro-background": "#FFFFFF", "intro-opacity": 98, "intro-border": "#FFFFFF", "intro-border-width": 2, "intro-shadow-opacity": 28, "intro-width": 65, "intro-x": 50, "intro-y": 27, "intro-padding": 40, "intro-radius": 32, "intro-alignment": "center", "intro-max-lines": 5 } },
      cinematic_dark: { label: "电影暗卡", values: { "intro-font": "Bahnschrift", "intro-headline-size": 70, "intro-headline-color": "#FFE06A", "intro-body-size": 32, "intro-body-color": "#F5F7FB", "intro-label-size": 23, "intro-label-color": "#FF8174", "intro-background": "#111827", "intro-opacity": 92, "intro-border": "#4B5870", "intro-border-width": 2, "intro-shadow-opacity": 44, "intro-width": 68, "intro-x": 50, "intro-y": 26, "intro-padding": 42, "intro-radius": 24, "intro-alignment": "center", "intro-max-lines": 5 } },
      romance_soft: { label: "柔光浪漫", values: { "intro-font": "Georgia", "intro-headline-size": 64, "intro-headline-color": "#FFF3F5", "intro-body-size": 31, "intro-body-color": "#4B3040", "intro-label-size": 23, "intro-label-color": "#B44972", "intro-background": "#FFF4F7", "intro-opacity": 94, "intro-border": "#F4C1D2", "intro-border-width": 2, "intro-shadow-opacity": 24, "intro-width": 68, "intro-x": 50, "intro-y": 28, "intro-padding": 42, "intro-radius": 42, "intro-alignment": "center", "intro-max-lines": 5 } },
      minimal_clean: { label: "纯净极简", values: { "intro-font": "Segoe UI", "intro-headline-size": 58, "intro-headline-color": "#FFFFFF", "intro-body-size": 30, "intro-body-color": "#17243C", "intro-label-size": 21, "intro-label-color": "#315BD8", "intro-background": "#FFFFFF", "intro-opacity": 88, "intro-border": "#D5DEEA", "intro-border-width": 1, "intro-shadow-opacity": 14, "intro-width": 62, "intro-x": 50, "intro-y": 29, "intro-padding": 34, "intro-radius": 12, "intro-alignment": "left", "intro-max-lines": 5 } },
      social_post: { label: "社交帖卡" },
      paper_note: { label: "纸张便笺" },
      golden_luxe: { label: "金色质感" },
      suspense_red: { label: "悬疑红卡" },
      blue_glass: { label: "蓝色玻璃" },
      warm_story: { label: "暖调故事" },
    },
    subtitle: {
      clear_outline: { label: "清晰描边", values: { "subtitle-font": "Arial", "subtitle-size": 52, "subtitle-color": "#FFFFFF", "subtitle-outline": "#101828", "subtitle-outline-width": 4, "subtitle-shadow-width": 4, "subtitle-background": "#101828", "subtitle-background-opacity": 0, "subtitle-alignment": "center", "subtitle-position-x": 50, "subtitle-bold": true, "subtitle-italic": false, "subtitle-word-sync": false } },
      cinematic_shadow: { label: "电影阴影", values: { "subtitle-font": "Segoe UI", "subtitle-size": 50, "subtitle-color": "#FFFFFF", "subtitle-outline": "#0A0F1B", "subtitle-outline-width": 2, "subtitle-shadow-width": 9, "subtitle-background": "#111827", "subtitle-background-opacity": 0, "subtitle-alignment": "center", "subtitle-position-x": 50, "subtitle-bold": true, "subtitle-italic": false, "subtitle-word-sync": false } },
      clean_minimal: { label: "极简阅读", values: { "subtitle-font": "Segoe UI", "subtitle-size": 48, "subtitle-color": "#FFFFFF", "subtitle-outline": "#101828", "subtitle-outline-width": 1, "subtitle-shadow-width": 3, "subtitle-background": "#101828", "subtitle-background-opacity": 62, "subtitle-alignment": "left", "subtitle-position-x": 50, "subtitle-bold": true, "subtitle-italic": false, "subtitle-word-sync": false } },
      bold_drama: { label: "强力戏剧", values: { "subtitle-font": "Bahnschrift", "subtitle-size": 56, "subtitle-color": "#FFFFFF", "subtitle-outline": "#000000", "subtitle-outline-width": 5, "subtitle-shadow-width": 5, "subtitle-background": "#101828", "subtitle-background-opacity": 84, "subtitle-alignment": "center", "subtitle-position-x": 50, "subtitle-bold": true, "subtitle-italic": false, "subtitle-word-sync": false } },
      reader_focus: { label: "阅读聚焦", values: { "subtitle-font": "Georgia", "subtitle-size": 49, "subtitle-color": "#FFFDF7", "subtitle-outline": "#15233D", "subtitle-outline-width": 3, "subtitle-shadow-width": 5, "subtitle-background": "#15233D", "subtitle-background-opacity": 34, "subtitle-alignment": "center", "subtitle-position-x": 50, "subtitle-bold": true, "subtitle-italic": false, "subtitle-word-sync": false } },
      soft_box: { label: "柔和底板", values: { "subtitle-font": "Segoe UI", "subtitle-size": 48, "subtitle-color": "#17243C", "subtitle-outline": "#FFFFFF", "subtitle-outline-width": 0, "subtitle-shadow-width": 2, "subtitle-background": "#F7F9FC", "subtitle-background-opacity": 86, "subtitle-alignment": "center", "subtitle-position-x": 50, "subtitle-bold": true, "subtitle-italic": false, "subtitle-word-sync": false } },
      word_pop_sync: { label: "逐词弹出", values: { "subtitle-font": "Bahnschrift", "subtitle-size": 52, "subtitle-color": "#FFFFFF", "subtitle-outline": "#101828", "subtitle-outline-width": 4, "subtitle-shadow-width": 5, "subtitle-background": "#101828", "subtitle-background-opacity": 12, "subtitle-alignment": "center", "subtitle-position-x": 50, "subtitle-bold": true, "subtitle-italic": false, "subtitle-word-sync": true, "subtitle-unread-color": "#FFFFFF", "subtitle-active-color": "#FFE06A", "subtitle-read-color": "#FFFFFF", "subtitle-pop-intensity": 65, "subtitle-pop-scale": 112, "subtitle-pop-duration": 160 } },
      romance_glow: { label: "浪漫柔光" },
      suspense_noir: { label: "悬疑黑金" },
      confession_clean: { label: "对白清透" },
      golden_hook: { label: "金色钩子" },
      midnight_reader: { label: "午夜阅读" },
      minimal_bottom: { label: "底部极简" },
    },
    code: {
      brand_pill: { label: "品牌胶囊", values: { "code-font": "Arial", "code-size": 42, "code-bold": true, "code-color": "#FFFFFF", "code-background": "#2446C8", "code-opacity": 92, "code-outline": "#FFFFFF", "code-outline-width": 1, "code-padding": 20, "code-radius": 12, "code-alignment": "center", "code-width": 62, "code-x": 50, "code-y": 9 } },
      dark_glass: { label: "深色玻璃", values: { "code-font": "Segoe UI", "code-size": 40, "code-bold": true, "code-color": "#FFFFFF", "code-background": "#111827", "code-opacity": 72, "code-outline": "#FFFFFF", "code-outline-width": 1, "code-padding": 19, "code-radius": 18, "code-alignment": "center", "code-width": 64, "code-x": 50, "code-y": 9 } },
      light_chip: { label: "浅色标签", values: { "code-font": "Segoe UI", "code-size": 38, "code-bold": true, "code-color": "#17243C", "code-background": "#FFFFFF", "code-opacity": 90, "code-outline": "#D0DAE7", "code-outline-width": 1, "code-padding": 18, "code-radius": 8, "code-alignment": "center", "code-width": 58, "code-x": 50, "code-y": 9 } },
      outline_only: { label: "纯描边", values: { "code-font": "Bahnschrift", "code-size": 40, "code-bold": true, "code-color": "#FFFFFF", "code-background": "#101828", "code-opacity": 18, "code-outline": "#FFFFFF", "code-outline-width": 3, "code-padding": 17, "code-radius": 8, "code-alignment": "center", "code-width": 60, "code-x": 50, "code-y": 9 } },
      warning_red: { label: "醒目红条" },
      golden_ticket: { label: "金色票签" },
      romance_blush: { label: "浪漫粉签" },
      minimal_dark: { label: "极简暗条" },
    },
    outro: {
      editorial_white: { label: "杂志白卡", values: { "outro-font": "Arial", "outro-title-size": 62, "outro-title-color": "#17243C", "outro-body-size": 32, "outro-body-color": "#53627A", "outro-code-size": 42, "outro-code-color": "#315BD8", "outro-background": "#FFFFFF", "outro-opacity": 98, "outro-border": "#D0DAE7", "outro-border-width": 2, "outro-width": 70, "outro-height": 38, "outro-x": 50, "outro-y": 31, "outro-padding": 42, "outro-radius": 32, "outro-alignment": "center" } },
      cinematic_dark: { label: "电影暗卡", values: { "outro-font": "Bahnschrift", "outro-title-size": 66, "outro-title-color": "#FFFFFF", "outro-body-size": 31, "outro-body-color": "#DCE3EF", "outro-code-size": 43, "outro-code-color": "#FFE06A", "outro-background": "#101828", "outro-opacity": 94, "outro-border": "#4A5871", "outro-border-width": 2, "outro-width": 72, "outro-height": 40, "outro-x": 50, "outro-y": 30, "outro-padding": 44, "outro-radius": 24, "outro-alignment": "center" } },
      brand_focus: { label: "品牌聚焦", values: { "outro-font": "Segoe UI", "outro-title-size": 58, "outro-title-color": "#FFFFFF", "outro-body-size": 30, "outro-body-color": "#EAF0FF", "outro-code-size": 48, "outro-code-color": "#FFFFFF", "outro-background": "#315BD8", "outro-opacity": 96, "outro-border": "#FFFFFF", "outro-border-width": 1, "outro-width": 72, "outro-height": 39, "outro-x": 50, "outro-y": 31, "outro-padding": 42, "outro-radius": 34, "outro-alignment": "center" } },
      minimal_clean: { label: "纯净极简", values: { "outro-font": "Segoe UI", "outro-title-size": 54, "outro-title-color": "#17243C", "outro-body-size": 29, "outro-body-color": "#53627A", "outro-code-size": 40, "outro-code-color": "#315BD8", "outro-background": "#FFFFFF", "outro-opacity": 88, "outro-border": "#FFFFFF", "outro-border-width": 0, "outro-width": 64, "outro-height": 34, "outro-x": 50, "outro-y": 32, "outro-padding": 34, "outro-radius": 10, "outro-alignment": "left" } },
    },
  };
  const coverAnimationCatalog = {
    gentle_push: { label: "舒缓推进", proof: "由远至近聚焦人物" },
    gentle_pull: { label: "缓慢拉远", proof: "由细节回到完整封面" },
    slow_pan: { label: "横向慢移", proof: "缓慢浏览横向构图" },
    soft_parallax: { label: "轻柔视差", proof: "细微漂移增加层次" },
    vertical_drift: { label: "纵向漂移", proof: "由上至下浏览完整封面" },
    focus_reveal: { label: "聚焦揭示", proof: "由柔焦逐渐进入清晰" },
    cinematic_push: { label: "电影推进", proof: "更有力度的平滑推进" },
    ken_burns_left: { label: "左向运镜", proof: "镜头缓慢向左移动" },
    ken_burns_right: { label: "右向运镜", proof: "镜头缓慢向右移动" },
    soft_flash: { label: "柔光闪现", proof: "柔和白光带入高潮结尾" },
    fade: { label: "柔和淡入", proof: "克制淡入，不抢字幕" },
    none: { label: "静态封面", proof: "无运动，渲染最快" },
  };
  const productionPresetSettingKeys = new Set([
    "retention_min", "retention_max", "adult_mode", "narration_wpm",
    "chapter_pause_seconds", "output_width", "output_height", "output_fps",
    "bgm_volume", "subtitle", "intro_card", "code_card", "outro_card",
    "caption_mode", "subtitle_preset", "intro_card_preset", "code_card_preset",
    "outro_card_preset", "subtitle_animation", "intro_animation",
    "max_episode_minutes", "cover_outro_enabled", "cover_animation", "color_grade", "end_card_seconds",
    "render_mode", "video_template", "output_mode", "export_narration_audio",
    "video_playback_speed", "video_transition", "subtitle_word_mode", "bgm_mode",
  ]);
  const productionPreferenceSettingKeys = new Set([
    ...productionPresetSettingKeys,
    "bgm_file",
  ]);

  const PRODUCTION_PREFERENCE_STORAGE_KEY = "storyforge.production-preferences.v1";
  const PRODUCTION_OUTPUT_MODES = new Set(["video_and_mp3", "audio_only", "reuse_audio"]);
  const PRODUCTION_VIDEO_SPEED_PRESETS = [1.0, 1.1, 1.25, 1.4, 1.5];
  const PRODUCTION_WPM_PRESETS = [220, 240, 260, 280];
  const PRODUCTION_WPM_LABELS = {
    220: "舒适",
    240: "推荐",
    260: "快速",
    280: "极快",
  };

  function normalizedProductionOutputMode(value) {
    const mode = String(value || "").trim().toLocaleLowerCase();
    return PRODUCTION_OUTPUT_MODES.has(mode) ? mode : "video_and_mp3";
  }

  function productionModeLabel(mode) {
    return {
      video_and_mp3: "常规视频生成",
      audio_only: "仅生成配音",
      reuse_audio: "已有配音更换素材",
    }[normalizedProductionOutputMode(mode)];
  }

  function productionSpeedLabel(value) {
    const speed = Number(value || 1);
    return Number.isInteger(speed)
      ? speed.toFixed(1)
      : speed.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
  }

  function normalizedCoverAnimation(value) {
    const key = String(value || "").trim();
    return coverAnimationCatalog[key] ? key : "gentle_push";
  }

  function normalizeSoftwareRole(role) {
    // Old Hub servers may still return "supervisor". Show it at the safer
    // employee level and lock editing until the host performs the v4 migration.
    return role === "admin" ? "admin" : "producer";
  }

  function softwareRoleLabel(role) {
    return softwareRoleCatalog[normalizeSoftwareRole(role)].label;
  }

  function isEmployeeSession() {
    const user = state.webSession?.user;
    return Boolean(user) && normalizeSoftwareRole(user.role) === "producer";
  }
  const isWebRuntime = ["http:", "https:"].includes(window.location.protocol);
  const isBrowserDemo = isWebRuntime && new URLSearchParams(window.location.search).get("demo") === "1";
  const draftFolderCatalog = {
    video_folder: {
      label: "视频素材",
      defaultLabel: "Hub 默认视频库",
      desktopPlaceholder: "悬疑 / 浪漫 / 悲伤 / 爽文分类文件夹",
      help: "读取已分类的视频素材；渲染时会自动删除素材原声。",
    },
    music_folder: {
      label: "背景音乐",
      defaultLabel: "Hub 默认音乐库",
      desktopPlaceholder: "选择分类音乐文件夹",
      help: "读取背景音乐，并在旁白出现时自动降低音量。",
    },
    output_folder: {
      label: "输出文件夹",
      defaultLabel: "Hub 默认成片目录",
      desktopPlaceholder: "成片保存位置",
      help: "成片、旁白和字幕对齐文件统一保存在这里。",
    },
  };
  const webRuntime = {
    csrfToken: "",
    loginInProgress: false,
    workerConnecting: null,
    desktopInitializing: null,
    desktopInitialized: false,
  };
  const LOCAL_WORKER_PORTS = [18765, 18766, 18767, 18768, 18769, 18770];
  // Protocol 2 is the V0.4 production contract (durable queue status,
  // self-check details and explicit MP4+MP3/audio-only output modes). Do not
  // let a stale V0.3 worker accept a V0.4 browser job halfway.
  const LOCAL_WORKER_PROTOCOL_VERSION = 2;
  const LOCAL_WORKER_MIN_COMPATIBLE_PROTOCOL_VERSION = 2;
  const localWorkerRpcMethods = new Set([
    "queue_production_draft",
    "generate_voice_candidates",
    "set_local_tts_provider",
    "start_queue",
    "cancel_queue",
    "get_jobs",
    "get_queue_connection",
    "get_archived_jobs",
    "retry_failed",
    "archive_batch",
    "restore_batch",
    "archive_job",
    "restore_job",
    "archive_finished_jobs",
    "clear_finished_jobs",
    "get_record_artifacts",
    "cancel_production_records",
    "open_output_folder",
    "choose_folder",
    "worker_profile",
    "worker_runtime_snapshot",
    "worker_self_check",
    "worker_set_folders",
  ]);
  const MOCK_PREVIEW_VIDEO_URI = "data:video/mp4;base64,AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAQtbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAdTAAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAA1h0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAdTAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAFoAAACgAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAHUwAACAAAABAAAAAALQbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAABAAAAHgABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAACe21pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAjtzdGJsAAAAw3N0c2QAAAAAAAAAAQAAALNhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAFoAoABIAAAASAAAAAAAAAABFUxhdmM2MS4xOS4xMDAgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAAOWF2Y0MBZAAM/+EAG2dkAAyscgRGFeTwEQAAAwABAAADAAIPFCmEYAEAB2joQ4EEsiz9+PgAAAAAEHBhc3AAAAABAAAAAQAAABRidHJ0AAAAAAAAA04AAANOAAAAGHN0dHMAAAAAAAAAAQAAAB4AAEAAAAAAFHN0c3MAAAAAAAAAAQAAAAEAAACIY3R0cwAAAAAAAAAPAAAAAQAAgAAAAAABAAKAAAAAAAEAAQAAAAAAAwAAAAAAAAAEAABAAAAAAAEAAoAAAAAAAQABAAAAAAADAAAAAAAAAAQAAEAAAAAAAQACgAAAAAABAAEAAAAAAAMAAAAAAAAABAAAQAAAAAABAADAAAAAAAEAAEAAAAAAHHN0c2MAAAAAAAAAAQAAAAEAAAAeAAAAAQAAAIxzdHN6AAAAAAAAAAAAAAAeAAADPAAAAIcAAABxAAAAQgAAAEMAAAA9AAAAQwAAAEQAAABFAAAAQwAAAIYAAABxAAAAQgAAAEIAAABCAAAARAAAAEUAAABGAAAAQwAAAIwAAAB4AAAAQwAAAEQAAABCAAAAQwAAAEMAAABGAAAAQwAAAHYAAABCAAAAFHN0Y28AAAAAAAAAAQAABF0AAABhdWR0YQAAAFltZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAACxpbHN0AAAAJKl0b28AAAAcZGF0YQAAAAEAAAAATGF2ZjYxLjcuMTAwAAAACGZyZWUAAAxwbWRhdAAAAq8GBf//q9xF6b3m2Ui3lizYINkj7u94MjY0IC0gY29yZSAxNjQgcjMxOTIgYzI0ZTA2YyAtIEguMjY0L01QRUctNCBBVkMgY29kZWMgLSBDb3B5bGVmdCAyMDAzLTIwMjQgLSBodHRwOi8vd3d3LnZpZGVvbGFuLm9yZy94MjY0Lmh0bWwgLSBvcHRpb25zOiBjYWJhYz0xIHJlZj0xNiBkZWJsb2NrPTE6MDowIGFuYWx5c2U9MHgzOjB4MTMzIG1lPXVtaCBzdWJtZT0xMCBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0xIG1lX3JhbmdlPTI0IGNocm9tYV9tZT0xIHRyZWxsaXM9MiA4eDhkY3Q9MSBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hyb21hX3FwX29mZnNldD0tMiB0aHJlYWRzPTUgbG9va2FoZWFkX3RocmVhZHM9MSBzbGljZWRfdGhyZWFkcz0wIG5yPTAgZGVjaW1hdGU9MSBpbnRlcmxhY2VkPTAgYmx1cmF5X2NvbXBhdD0wIGNvbnN0cmFpbmVkX2ludHJhPTAgYmZyYW1lcz04IGJfcHlyYW1pZD0yIGJfYWRhcHQ9MiBiX2JpYXM9MCBkaXJlY3Q9MyB3ZWlnaHRiPTEgb3Blbl9nb3A9MCB3ZWlnaHRwPTIga2V5aW50PTI1MCBrZXlpbnRfbWluPTEgc2NlbmVjdXQ9NDAgaW50cmFfcmVmcmVzaD0wIHJjX2xvb2thaGVhZD02MCByYz1jcmYgbWJ0cmVlPTEgY3JmPTQyLjAgcWNvbXA9MC42MCBxcG1pbj0wIHFwbWF4PTY5IHFwc3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MToxLjAwAIAAAACFZYiBAAN//sdv5lkjohA8YFytCTLC1uzN1LTHfNpQg9qKBG+VoBm4XWrg6pVCNXwgBA6jTtMVweYxFUCc+fuI35OHNj+UtDT9pmVQ3stfFpElIqsANznNdq60v/656H34Dx6dxbj6j32bj7HRucBZ4IiVnZfPVHb8kpM4N90acEMGirlYnQAAAINBmgktiDX/d8UZvA9PofOLxcXoKHB4KZuWMX09LIdZ35AMv3ta5W4WYx2WMES0mazevXkpRNwDuxdf+oP4DGXYcTrHTupfbPhJcPpKO3AuEu7+RrXXNrmnDrI4Tt7pXjq9p0T7D4BZVoASWQupB6KeUJ2D0El56x+nvKCcTusgwpMd3gAAAG1BnhCHEGP//vx8/A4Uwlj2iMe5Inw/8jvqB+1rLZRP05PiYywrlqjRhoCAFYbn5z7EaW7dhejhFLM9wAc6UCV+RSkXz78TmfROwUirc2smLQAKjq1rDAa3q4tScQud++IK8OM8BO/zC8qwUsDFAAAAPgGeGCaIK//+00yrOaukerP3GB3WmXbpFJLjZQ/zhggu4u2PmHSOSInOpsFnV7doFc0kxGzx8mu+FkI1UcIQAAAAPwGeGEaIK//+00yrOaukerP3GB3WmXbpFJLjZQ/zivJTwEf7xECW/1b9OXxywaT5Efh+cmViji0IEpmnuuZOkQAAADkBnhhmiCv//tNL8ucgj8tkB0HdaZdukUkuNlD/OGPuYiT/ZX3gqXufdIGviCAssH3905Bl776i6d8AAAA/AZ4YrUgr//7TTCl0fHpHqz9xgd1pl26RSS42UP84rd1m7UABCjkN09f69y9/7q8JLEL4OSk4CxH2rQVFgrQNAAAAQAGeGM1IK//+00yti3zkEflsgOg7rTLt0iklxsof5w5l3t2oACFHIbp6/17l7/3V4SWIXwemY463mjh0FRYK0DEAAABBAZ4Y7Ugr//7TTK2cG9XSPVn7jA7rTLt0iklxsof5w24TgI/3iIEt/q36cvjlg0nyI/D84PzFJOu6AGPdSD/yn0AAAAA/AZ4ZDUgr//7TTCl0fHpHqz9xgd1pl26RSS42UP834IMNjZurS8GyROCoNgtPbbtArle3gGzx8mu+FkI1UcIQAAAAgkGaGkk1AgLRMpgQZ/9n1U+80U5UN4nwsiG1UL15dUYz0iNdgfbAnDj+Axj8XzMjsvm00ROVmYPY2dwamR0pmG1PVFD2/yzUFmpOrmI65L+nEnuLn33Gjum6YmOq6XrlIOWLDB/vHYnb0c9W0jPfS+pNs+TvZb5+JXkPLMqG/poAWcEAAABtQZ4hpcQY//78fPw94DlPzIjHuSJ8P/I76gftay2Uf7rBHdulM20BmfGQcFlw8QjqbpZBBBV6QpAw5h3XL5QZgbXKfShliWdnIpfMc3JLsJ+U8BXT1TaIaPwktSJZBonl6zqFb5wteX0OAfRKyQAAAD4BnilFogr//tMphDDARHqz9xgd1pl26RSS42UP834DNKPjz4Ns3KvzIuAkqfQiARWGPh+jaOTXfCyEaqOEIAAAAD4Bnillogr//tLgjSYCI9WfuMDutMu3SKSXGyh/nAPuBOkf7xECW/1b9OXxywaT5Efh+cTiBsWhAlM091zJ0wAAAD4BnimFogr//tLgJ5AIj1Z+4wO60y7dIpJcbKH+cSJx4CP94iBLf6t+nL45YNJ8iPw/OIfJc8gAY91IP/KfQQAAAEABninMkgr//tNMffR8ekerP3GB3WmXbpFJLjZQ/ziafYd0j/eIgS3+rfpy+OWDSfIj8Pzg7kTaLQgSmae65k6RAAAAQQGeKeySCv/+00zM63zkEflsgOg7rTLt0iklxsof5xXZMk6R/vEQJb/Vv05fHLBpPkR+H5wfGY4l3QAx7qQf+U+gAAAAQgGeKgySCv/+00z2cudV0j1Z+4wO60y7dIpJcbKH+cWQYDwEf7xECW/1b9OXxywaT5Efh+cHTheZDYRd+lgL+FOMxgAAAD8Bnioskgr//tNMffR8ekerP3GB3WmXbpFJLjZQ/ziJoRx4PSdWz+So3dZsuRE7doFcwO8A2ePk13wshGqjhCEAAACIQZorabUCAtrRMpgBBf9fYrN5odeM8dVjzQaUhY7ns4VEgIggGVIQExeMzzotZ4sEbJhGF4wePLJyRBAyWUXVeZ57qnWw2jmsFbnNCQ8HkIJ6prL3fBfvpxyW8IwZV3wG88vV++lZnUDwT+ODWSthvdrI+CArRyCq6rbUAmCBO3LGmUDxKZbMwAAAAHRBnjLEsQY//vx8/A5DohiguVPoNKD9SR31A/a1lson6pXM8g5/SFTaMNAR+yG4Al7EZs2rqnhFLM/0UX250sUr8ilIu2RiPKFaT4MJcVoDrY0uChCpT/Vsa6pH8zCQomhjgA6E73OKQPxSFkcd4dEN/mahsAAAAD8BnjpkqIL//tNMyHmrpHqz9xgd1pl26RSS42UP84sDg8BH+8RAlv9W/Tl8csGk+RH4fFHOXwpeAh4wUBskBw0AAABAAZ46hKiCv/7TTPJ08gj8tkB0HdaZdukUkuNlD/OLuL5Okf7xECW/1b9OXxywaT5Efh+coLXBxIyJijAX8KcZjAAAAD4BnjqkqIL//tNMR2cgj8tkB0HdaZdukUkuNlD/OLtoe3agAIUchunr/XuXv/dXhJYhfJixA8gRXtZPznL0gQAAAD8Bnjrs0gr//tNMfiC8ekerP3GB3WmXbpFJLjZQ/zivncbtQAEKOQ3T1/r3L3/urwksQvg7lggLEfatBUWCtA0AAAA/AZ47DNIK//7TTMzxHOQR+WyA6DutMu3SKSXGyh/PoIBwA4dpjXScmaj9C9f3gh1CswZGTdN8GJW5DAdX6JcDAAAAQgGeOyzSCv/+00zM/RvV0j1Z+4wO60y7dIpJcbKH+cTWHonSP94iBLf6t+nL45YNJ8iPw/ODpxvNCCMiYowF/CnGYwAAAD8BnjtM0gr//tNMfiC8ekerP3GB3WmXbpFJLjZQ/ziXrHAR/vEQJb/Vv05fHLBpPkR+H5websGi0IEpmnuuZOkAAAByQZo7qI1AgLa2tEymAAQV/0qFVHM+TlqGI9fovB9TkZpaRQ3IA7iWMjfEiiXL/TZaLFH0DtLpdv3GKiZ1GY0yi5uFVt80Ux9CrkdgSUcIHFDz/2DFlephUYYcLVhBKS4c3+R1uT7dj0syscUPCwUNZxLVAAAAPgGeQ4TyCv/+00BQAER6s/cYHdaZdukUkuNlD/N/cqlbg9GLgtBCoou+4QChr9g8aYfm46Y8uqJZbW8eiC3B";
  const MOCK_NARRATION_AUDIO_URI = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";

  function mockCoverDataUri(title, start = "#2f54d0", end = "#14213d") {
    const safeTitle = String(title || "Story").replace(/[<>&]/g, "").slice(0, 28);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="360" height="540"><defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="${start}"/><stop offset="1" stop-color="${end}"/></linearGradient></defs><rect width="100%" height="100%" fill="url(#g)"/><circle cx="285" cy="95" r="105" fill="white" opacity=".12"/><path d="M25 420 C120 310 215 520 340 350" fill="none" stroke="white" stroke-width="18" opacity=".15"/><text x="28" y="290" fill="white" font-family="Arial" font-size="32" font-weight="700">${safeTitle}</text><text x="28" y="330" fill="white" opacity=".72" font-family="Arial" font-size="14">STORYFORGE SERIAL</text></svg>`;
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  }

  const mockBootstrap = {
    settings: {
      language: "en-US",
      retention_min: 0.85,
      retention_max: 0.9,
      adult_mode: "engaging",
      narration_wpm: 240,
      chapter_pause_seconds: 0.8,
      output_width: 1080,
      output_height: 1920,
      output_fps: 60,
      bgm_volume: 0.28,
      caption_mode: "semantic",
      video_template: "classic",
      intro_card_preset: "editorial_white",
      subtitle_preset: "clear_outline",
      code_card_preset: "brand_pill",
      outro_card_preset: "editorial_white",
      subtitle_animation: "none",
      intro_animation: "fade_rise",
      preview_seconds: DEFAULT_PREVIEW_SECONDS,
      render_mode: "speed",
      cover_outro_enabled: true,
      cover_animation: "gentle_push",
      color_grade: "neutral",
      voice_by_mood: {
        suspense: "dramatic",
        romance: "warm",
        sad: "calm",
        revenge: "confident",
      },
      subtitle: {
        font_family: "Arial",
        font_size: 52,
        text_color: "#FFFFFF",
        outline_color: "#101828",
        outline_width: 4,
        bottom_margin: 310,
        horizontal_margin: 180,
        max_chars_per_line: 28,
        max_lines: 3,
        bold: true,
        italic: false,
        shadow_width: 4,
        background_color: "#101828",
        background_opacity: 0,
        alignment: "center",
        position_x_percent: 50,
        word_sync_enabled: false,
        unread_color: "#FFFFFF",
        active_color: "#FFE06A",
        read_color: "#FFFFFF",
        pop_scale: 112,
        pop_duration_ms: 160,
        pop_intensity: 0.65,
      },
      intro_card: {
        font_family: "Arial",
        headline_font_size: 66,
        headline_color: "#FFE06A",
        body_font_size: 32,
        body_color: "#263247",
        label_font_size: 24,
        label_color: "#315BD8",
        background_color: "#FFFFFF",
        background_opacity: 0.98,
        border_color: "#FFFFFF",
        border_width: 2,
        shadow_opacity: 0.28,
        width_percent: 65,
        position_x_percent: 50,
        position_y_percent: 27,
        padding: 40,
        radius: 32,
        text_alignment: "center",
        max_lines: 5,
      },
      code_card: {
        font_family: "Arial",
        font_size: 42,
        text_color: "#FFFFFF",
        background_color: "#2446C8",
        opacity: 0.92,
        top_margin: 180,
        horizontal_margin: 150,
        bold: true,
        outline_color: "#FFFFFF",
        outline_width: 1,
        alignment: "center",
        position_x_percent: 50,
        position_y_percent: 9,
        width_percent: 62,
        padding: 20,
        radius: 12,
      },
      outro_card: {
        font_family: "Arial",
        title_font_size: 62,
        title_color: "#17243C",
        body_font_size: 32,
        body_color: "#53627A",
        code_font_size: 42,
        code_color: "#315BD8",
        background_color: "#FFFFFF",
        background_opacity: 0.98,
        border_color: "#D0DAE7",
        border_width: 2,
        width_percent: 70,
        height_percent: 38,
        position_x_percent: 50,
        position_y_percent: 31,
        padding: 42,
        radius: 32,
        text_alignment: "center",
      },
      providers: {
        text_provider: "local",
        text_model: "",
        text_endpoint: "",
        text_api_key: "",
        tts_provider: "local_kokoro",
        tts_endpoint: "",
        tts_api_key: "",
        kokoro_endpoint: "",
        kokoro_command: "",
        monthly_character_limit: 0,
        allow_provider_fallback: true,
      },
      hub: {
        mode: "host",
        endpoint: "http://127.0.0.1:8765",
        access_token: "",
        has_access_token: false,
        account_username: "",
        device_name: "Studio-PC-01",
        listen_host: "0.0.0.0",
        listen_port: 8765,
        share_previews: false,
        share_narration: false,
        auto_update_enabled: true,
        auto_download_updates: true,
        update_check_minutes: 1,
        web_allowed_roots: [
          "D:\\StoryForgeMedia",
          "D:\\StoryForgeProjects",
        ],
      },
    },
    web_default_folders: {
      video_folder: "D:\\StoryForgeMedia\\videos",
      music_folder: "D:\\StoryForgeMedia\\music",
      output_folder: "D:\\StoryForgeProjects\\output",
    },
    update_status: {
      current_version: "0.4.0-rc3",
      available_version: "",
      state: "up_to_date",
      message: "当前已经是主机提供的最新版本。",
      checked_at: "2026-07-24T13:55:00Z",
      package_path: "",
      downloaded: false,
      apply_on_restart: false,
      restart_required: false,
      rendering_busy: false,
      error: "",
    },
    hub_status: {
      configured_mode: "host",
      runtime_mode: "host",
      mode: "host",
      online: true,
      connected: true,
      running: true,
      status: "ready",
      message: "主电脑服务运行正常，制作电脑可以连接。",
      endpoint: "http://127.0.0.1:8765",
      device_name: "Studio-PC-01",
      restart_required: false,
    },
    device_sync: {
      state: "ready",
      enabled: true,
      device_id: "device-render-01",
      applied_revision_id: "config-revision-2",
      last_success_at: "2026-07-27T08:42:00Z",
      last_error: "",
      poll_seconds: 20,
    },
    managed_devices: [
      {
        id: "device-render-01",
        name: "剪辑电脑-01",
        hostname: "EDIT-01",
        app_version: "0.3.2",
        os_name: "Windows",
        architecture: "AMD64",
        capabilities: { local_render: true, local_tts: true, local_subtitles: true },
        last_user_id: "user-renderer-01",
        active: true,
        online: true,
        first_seen_at: "2026-07-22T02:12:00Z",
        last_seen_at: "2026-07-27T08:42:00Z",
        active_token_count: 1,
        desired_revision_number: 2,
      },
      {
        id: "device-render-02",
        name: "剪辑电脑-02",
        hostname: "EDIT-02",
        app_version: "0.3.1",
        os_name: "Windows",
        architecture: "AMD64",
        capabilities: { local_render: true, local_tts: true, local_subtitles: true },
        last_user_id: "user-renderer-01",
        active: true,
        online: false,
        first_seen_at: "2026-07-23T03:24:00Z",
        last_seen_at: "2026-07-27T06:20:00Z",
        active_token_count: 1,
        desired_revision_number: 2,
      },
      {
        id: "device-old-01",
        name: "备用电脑",
        hostname: "SPARE-PC",
        app_version: "0.3.0",
        os_name: "Windows",
        architecture: "AMD64",
        capabilities: { local_render: true },
        last_user_id: "user-renderer-01",
        active: false,
        online: false,
        first_seen_at: "2026-07-20T04:30:00Z",
        last_seen_at: "2026-07-24T01:20:00Z",
        active_token_count: 0,
        desired_revision_number: 1,
      },
    ],
    managed_device_configs: [
      {
        id: "config-revision-2",
        revision_number: 2,
        config_schema_version: 1,
        config: { language: "en-US", narration_wpm: 240, bgm_volume: 0.28, output_fps: 60 },
        config_hash: "mock-config-hash-2",
        target_mode: "all",
        target_count: 2,
        note: "统一 60 FPS 与旁白节奏",
        created_by_user_id: "user-owner",
        created_at: "2026-07-27T08:35:00Z",
        targets: [
          { device_id: "device-render-01", device_name: "剪辑电脑-01", device_active: true, assigned_at: "2026-07-27T08:35:00Z", acknowledged_at: "2026-07-27T08:36:00Z", ack_status: "applied", ack_message: "" },
          { device_id: "device-render-02", device_name: "剪辑电脑-02", device_active: true, assigned_at: "2026-07-27T08:35:00Z", acknowledged_at: "", ack_status: "", ack_message: "" },
        ],
      },
    ],
    platforms: [
      {
        id: "preview-goodnovel",
        name: "GoodNovel",
        search_template: "Search {platform}: {code}",
        ending_template: "Download {platform} and search code {code} to continue reading.",
        logo_path: "",
        logo_uri: "",
        brand_color: "#e53935",
      },
      {
        id: "preview-motonovel",
        name: "MotoNovel",
        search_template: "Open {platform} and enter {code}",
        ending_template: "Open {platform} and enter code {code} to keep reading.",
        logo_path: "",
        logo_uri: "",
        brand_color: "#8256d0",
      },
      {
        id: "preview-novelmaster",
        name: "Novel Master",
        search_template: "Find it on {platform}: {code}",
        ending_template: "Download {platform} and use code {code} for the full story.",
        logo_path: "",
        logo_uri: "",
        brand_color: "#ee4f58",
      },
    ],
    jobs: [
      {
        id: "preview-job-1",
        batch_id: "preview-batch",
        platform_id: "preview-goodnovel",
        title: "The Call at Ten",
        code: "B73165",
        source_file: "D:\\work\\book tools\\input txt\\B73165_The Call at Ten.txt",
        output_folder: "D:\\work\\book tools\\output",
        output_file: "",
        status: "queued",
        progress: 0,
        stage_label: "等待处理",
        archived: false,
      },
      {
        id: "preview-job-2",
        batch_id: "preview-batch",
        platform_id: "preview-goodnovel",
        title: "Her Husband's Secret",
        code: "A92K7",
        source_file: "D:\\work\\book tools\\input txt\\A92K7_Her Husband's Secret.txt",
        output_folder: "D:\\work\\book tools\\output",
        output_file: "D:\\work\\book tools\\output\\A92K7.mp4",
        status: "completed",
        progress: 1,
        stage_label: "已完成",
        archived: false,
      },
    ],
    system: {
      python: "3.12",
      ffmpeg_ready: true,
      ffmpeg_path: "Bundled FFmpeg",
      encoders: ["libx264"],
      recommended_encoder: "libx264",
      webview_runtime: "Browser preview",
    },
    production_presets: {
      total: 0,
      items: [],
    },
  };

  const mockLibraryBootstrap = {
    novels: [
      {
        id: "novel-starting-over",
        title: "Starting Over at Sixty Years Old",
        cover_path: "D:\\StoryForgeMedia\\covers\\starting-over.jpg",
        cover_uri: mockCoverDataUri("Starting Over at Sixty", "#ec6a53", "#18223c"),
        language: "en",
        language_detection: { code: "en", display_name: "英语", confidence: 0.99, source: "auto" },
        synopsis:
          "On her wedding anniversary, Evelyn finds a photo album that exposes the life her husband has hidden for decades. At sixty, she decides the next chapter will finally belong to her.",
        tags: ["Revenge", "Betrayal", "Divorce"],
        cover_tone: "ember",
        source_type: "docx",
        source_chapters: 8,
        estimated_duration_seconds: 2380,
        default_voice: "Warm American female",
        locked_voice_provider: "browser_speech",
        locked_voice_id: "ava-warm",
        voice_candidates: [
          { profile: "warm", label: "Ava · Warm suspense", provider: "browser_speech", voice_id: "ava-warm", audio_uri: "mock://speech/ava-warm", duration_seconds: 12, excerpt: "I thought I knew the man I married, until the album opened." },
          { profile: "intimate", label: "Maya · Intimate reveal", provider: "browser_speech", voice_id: "maya-intimate", audio_uri: "mock://speech/maya-intimate", duration_seconds: 12, excerpt: "Every photograph carried the same date, and the same hidden name." },
          { profile: "confident", label: "Sloane · Controlled revenge", provider: "browser_speech", voice_id: "sloane-confident", audio_uri: "mock://speech/sloane-confident", duration_seconds: 12, excerpt: "At sixty, she was done asking permission to begin again." },
        ],
        progress: { completed: 5, total: 8 },
        platform_bindings: [
          {
            platform_id: "preview-goodnovel",
            codes: [
              { id: "code-b56826", value: "B56826", active: true, use_count: 7 },
              { id: "code-b73165", value: "B73165", active: true, use_count: 4 },
            ],
          },
        ],
        episodes: [
          { id: "so-e1", number: 1, title: "The Anniversary Album", source_label: "Chapter 1–2", duration_seconds: 424, status: "completed" },
          { id: "so-e2", number: 2, title: "A Name in Every Photograph", source_label: "Chapter 3", duration_seconds: 392, status: "completed" },
          { id: "so-e3", number: 3, title: "The House Across Town", source_label: "Chapter 4", duration_seconds: 448, status: "failed" },
          { id: "so-e4", number: 4, title: "What He Signed Away", source_label: "Chapter 5–6", duration_seconds: 514, status: "ready" },
          { id: "so-e5", number: 5, title: "Her First Morning Free", source_label: "Chapter 7–8", duration_seconds: 486, status: "ready" },
        ],
        materials: [
          { id: "mat-rain", name: "Rainy window loop.mp4", type: "video", usage_count: 12 },
          { id: "mat-jewel", name: "Gem sorting 04.mp4", type: "video", usage_count: 4 },
          { id: "mat-piano", name: "Quiet tension.wav", type: "music", usage_count: 7 },
        ],
        draft: {
          id: "draft-starting-over",
          platform_id: "preview-goodnovel",
          promo_code_id: "code-b56826",
          publishing_account_id: "account-romance-01",
          episode_ids: ["so-e4", "so-e5"],
          variant_count: 3,
          approvals: { main: "pending", variants: {} },
          video_folder: "D:\\StoryForgeMedia\\videos",
          music_folder: "D:\\StoryForgeMedia\\music",
          output_folder: "D:\\StoryForgeMedia\\output",
        },
        updated_at: "2026-07-21T10:28:00Z",
      },
      {
        id: "novel-second-chance",
        title: "离婚后，他等了我十年",
        cover_path: "D:\\StoryForgeMedia\\covers\\second-chance.jpg",
        cover_uri: mockCoverDataUri("Second Chance at Love", "#8754c7", "#17213d"),
        language: "zh",
        language_detection: { code: "zh", display_name: "中文", confidence: 0.98, source: "auto" },
        synopsis:
          "一场公开离婚后，诺拉接受了旧识提供的临时工作，却发现那个男人已经默默等了她十年。",
        tags: ["Romance", "Second Chance"],
        cover_tone: "violet",
        source_type: "txt",
        source_chapters: 5,
        estimated_duration_seconds: 1480,
        default_voice: "中文女声 · 待试听",
        locked_voice_provider: "browser_speech",
        locked_voice_id: "maya-intimate",
        voice_candidates: [],
        progress: { completed: 2, total: 4 },
        platform_bindings: [
          {
            platform_id: "preview-motonovel",
            codes: [
              { id: "code-m88210", value: "M88210", active: true, use_count: 2 },
              { id: "code-m99142", value: "M99142", active: false, use_count: 0 },
            ],
          },
        ],
        episodes: [
          { id: "sc-e1", number: 1, title: "The Divorce Party", source_label: "Chapter 1", duration_seconds: 346, status: "completed" },
          { id: "sc-e2", number: 2, title: "The Man in the Corner Office", source_label: "Chapter 2", duration_seconds: 398, status: "completed" },
          { id: "sc-e3", number: 3, title: "One Temporary Contract", source_label: "Chapter 3–4", duration_seconds: 476, status: "ready" },
          { id: "sc-e4", number: 4, title: "What He Never Sent", source_label: "Chapter 5", duration_seconds: 366, status: "ready" },
        ],
        materials: [
          { id: "mat-sand", name: "Kinetic sand blue.mp4", type: "video", usage_count: 5 },
          { id: "mat-city", name: "Night city bokeh.mp4", type: "video", usage_count: 3 },
          { id: "mat-soft", name: "Soft resolve.mp3", type: "music", usage_count: 2 },
        ],
        draft: {
          id: "draft-second-chance",
          platform_id: "preview-motonovel",
          promo_code_id: "code-m88210",
          publishing_account_id: "",
          episode_ids: ["sc-e3"],
          variant_count: 2,
          approvals: { main: "approved", variants: { "2": "pending" } },
          video_folder: "D:\\StoryForgeMedia\\videos",
          music_folder: "D:\\StoryForgeMedia\\music",
          output_folder: "D:\\StoryForgeMedia\\output",
        },
        updated_at: "2026-07-21T09:06:00Z",
      },
      {
        id: "novel-redemption",
        title: "Redención de amor",
        cover_path: "",
        cover_uri: "",
        language: "es",
        language_detection: { code: "es", display_name: "西班牙语", confidence: 0.62, source: "auto" },
        synopsis:
          "Un matrimonio por contrato le da a Celeste un año para desenmascarar a la familia que destruyó la suya, pero el heredero empieza a ayudarla.",
        tags: ["Contract Marriage", "Revenge"],
        cover_tone: "noir",
        source_type: "paste",
        source_chapters: 3,
        estimated_duration_seconds: 840,
        default_voice: "西班牙语女声 · 待试听",
        locked_voice_provider: "",
        locked_voice_id: "",
        voice_candidates: [],
        progress: { completed: 0, total: 3 },
        platform_bindings: [],
        episodes: [
          { id: "lr-e1", number: 1, title: "The Contract", source_label: "Chapter 1", duration_seconds: 358, status: "ready" },
          { id: "lr-e2", number: 2, title: "A Photograph in the Safe", source_label: "Chapter 2", duration_seconds: 402, status: "ready" },
          { id: "lr-e3", number: 3, title: "The Wrong Family Name", source_label: "Chapter 3", duration_seconds: 330, status: "ready" },
        ],
        materials: [
          { id: "mat-marble", name: "Marble polish 02.mp4", type: "video", usage_count: 1 },
          { id: "mat-dark", name: "Low pulse.wav", type: "music", usage_count: 1 },
        ],
        draft: { id: "draft-redemption", platform_id: "", promo_code_id: "", publishing_account_id: "", episode_ids: ["lr-e1"], variant_count: 1, approvals: { main: "pending", variants: {} }, video_folder: "", music_folder: "", output_folder: "" },
        updated_at: "2026-07-20T16:42:00Z",
      },
    ],
    publishing_accounts: [
      { id: "account-romance-01", platform_id: "preview-goodnovel", name: "US Romance 01", handle: "@story.after.dark", active: true, row_version: 1 },
      { id: "account-revenge-02", platform_id: "preview-goodnovel", name: "US Revenge 02", handle: "@her.next.chapter", active: true, row_version: 1 },
      { id: "account-moto-01", platform_id: "preview-motonovel", name: "Moto Serial 01", handle: "@midnight.pages", active: true, row_version: 1 },
    ],
    users: [
      { id: "user-owner", username: "storyforge-owner", display_name: "主账号", role: "admin", active: true, row_version: 1, permission_overrides: {} },
      { id: "user-lead-01", username: "team-admin", display_name: "内容管理员", role: "admin", active: true, row_version: 1, permission_overrides: {} },
      { id: "user-renderer-01", username: "renderer-01", display_name: "制作员工 1", role: "producer", active: true, row_version: 1, permission_overrides: {} },
    ],
    production_records: [
      {
        id: "record-failed-so3",
        novel_id: "novel-starting-over",
        title: "Starting Over at Sixty Years Old",
        episode_label: "第3集",
        creative_line: 2,
        status: "failed",
        platform_id: "preview-goodnovel",
        promo_code: "B56826",
        publishing_account_name: "US Romance 01",
        stage_label: "渲染成片",
        progress: 0.82,
        error: "素材解码失败：Rainy window loop.mp4；任务已跳过，其余视频继续。",
        output_folder: "D:\\work\\book tools\\output",
        materials: [{ name: "Rainy window loop.mp4", usage_count: 12 }, { name: "Quiet tension.wav", usage_count: 7 }],
        created_at: "2026-07-21T10:12:00Z",
      },
      {
        id: "record-complete-so2",
        novel_id: "novel-starting-over",
        title: "Starting Over at Sixty Years Old",
        episode_label: "第2集",
        creative_line: 1,
        status: "completed",
        platform_id: "preview-goodnovel",
        promo_code: "B56826",
        publishing_account_name: "US Romance 01",
        stage_label: "已完成",
        progress: 1,
        artifact_count: 3,
        error: "",
        output_folder: "D:\\work\\book tools\\output",
        materials: [{ name: "Gem sorting 04.mp4", usage_count: 4 }, { name: "Quiet tension.wav", usage_count: 7 }],
        created_at: "2026-07-21T09:44:00Z",
      },
      {
        id: "record-active-sc2",
        novel_id: "novel-second-chance",
        title: "Second Chance at Love",
        episode_label: "第2集",
        creative_line: 1,
        status: "active",
        platform_id: "preview-motonovel",
        promo_code: "M88210",
        publishing_account_name: "待分配",
        stage_label: "生成旁白",
        progress: 0.46,
        error: "",
        output_folder: "D:\\work\\book tools\\output",
        materials: [{ name: "Kinetic sand blue.mp4", usage_count: 5 }],
        created_at: "2026-07-21T10:24:00Z",
      },
    ],
  };

  const browserMockState = isBrowserDemo ? structuredClone(mockBootstrap) : null;
  const browserMockLibrary = isBrowserDemo ? structuredClone(mockLibraryBootstrap) : null;

  function webHeaders({ json = false, mutating = false } = {}) {
    const headers = { Accept: "application/json" };
    if (json) headers["Content-Type"] = "application/json";
    if (mutating && webRuntime.csrfToken) headers["X-StoryForge-CSRF"] = webRuntime.csrfToken;
    return headers;
  }

  function rememberWebSessionData(data) {
    const session = data?.session || data || {};
    if (session.csrf_token) webRuntime.csrfToken = String(session.csrf_token);
    if (session.user) state.webSession = session;
    if (session.capabilities && typeof session.capabilities === "object") {
      state.webCapabilities = { ...state.webCapabilities, ...session.capabilities };
    }
    return session;
  }

  async function parseWebResponse(response) {
    const text = await response.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (_error) {
        payload = { ok: false, error: `网页服务返回了无法识别的内容（HTTP ${response.status}）。` };
      }
    }
    if (!payload) payload = response.ok ? { ok: true, data: null } : { ok: false, error: `网页请求失败（HTTP ${response.status}）。` };
    if (!response.ok && payload.ok !== false) payload.ok = false;
    if (!response.ok && !payload.error) payload.error = `网页请求失败（HTTP ${response.status}）。`;
    return payload;
  }

  function apiFailureCode(payload) {
    const nested = payload?.error && typeof payload.error === "object" ? payload.error : null;
    return String(payload?.code || payload?.error_code || nested?.code || "").trim();
  }

  function apiFailureMessage(payload, fallback = "操作失败。请重试。") {
    const nested = payload?.error && typeof payload.error === "object" ? payload.error : null;
    return String(nested?.message || payload?.message || (typeof payload?.error === "string" ? payload.error : "") || fallback).trim();
  }

  function responseRequiresClientUpdate(response, payload) {
    const code = apiFailureCode(payload);
    const message = apiFailureMessage(payload, "");
    return Number(response?.status || payload?.status || 0) === 426
      || code === "client_update_required"
      || /(?:client_update_required|HTTP\s*426|版本过旧[^。]*更新)/iu.test(message);
  }

  function showClientUpdateRequired(payload) {
    const alreadyShown = state.updateStatus?.state === "required";
    const message = apiFailureMessage(
      payload,
      "当前制作电脑版本过旧，必须更新 StoryForge 后才能领取新的制作任务。",
    );
    const status = {
      ...(state.updateStatus || {}),
      state: "required",
      code: "client_update_required",
      update_required: true,
      message,
      error: "",
      checked_at: new Date().toISOString(),
    };
    renderUpdateStatus(status);
    if (alreadyShown) return;
    const dialog = $("#employee-update-dialog");
    if (dialog && !dialog.open) {
      if (dialog.showModal) dialog.showModal();
      else dialog.setAttribute("open", "");
    }
    toast(`${message} 已在“软件更新”中为你打开处理入口。`, "error");
  }

  async function webRequest(path, { method = "GET", jsonBody = null, formData = null, allowUnauthorized = false } = {}) {
    const mutating = !["GET", "HEAD", "OPTIONS"].includes(String(method).toUpperCase());
    const response = await window.fetch(path, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      headers: webHeaders({ json: jsonBody !== null, mutating }),
      body: formData || (jsonBody !== null ? JSON.stringify(jsonBody) : undefined),
    });
    const result = await parseWebResponse(response);
    if (responseRequiresClientUpdate(response, result)) showClientUpdateRequired(result);
    if (result?.data && String(path).startsWith("/web/api/session")) rememberWebSessionData(result.data);
    if (response.status === 401 && !allowUnauthorized) {
      requireWebLogin("登录已失效，请重新登录后继续。 ");
    }
    return result;
  }

  async function webRpc(method, args) {
    return webRequest("/web/api/rpc", {
      method: "POST",
      jsonBody: { method, args },
    });
  }

  async function localWorkerFetch(baseUrl, path, options = {}, timeoutMs = 1800) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await window.fetch(`${baseUrl}${path}`, {
        cache: "no-store",
        ...options,
        signal: controller.signal,
      });
      const result = await parseWebResponse(response);
      if (responseRequiresClientUpdate(response, result)) showClientUpdateRequired(result);
      return { response, result };
    } finally {
      window.clearTimeout(timer);
    }
  }

  function localWorkerCompatibility(health) {
    const protocol = Number(health?.protocol_version);
    const minimumBrowser = Number(health?.minimum_browser_protocol_version);
    if (!Number.isInteger(protocol) || !Number.isInteger(minimumBrowser)) {
      return {
        compatible: false,
        code: "legacy_worker",
        message: "当前电脑运行的是旧版本机制作服务，缺少兼容信息。",
        fix: "关闭旧版 StoryForge，安装或更新当前电脑的 StoryForge，然后重新打开软件。",
      };
    }
    if (protocol < LOCAL_WORKER_MIN_COMPATIBLE_PROTOCOL_VERSION) {
      return {
        compatible: false,
        code: "worker_too_old",
        message: `当前电脑的本机制作服务协议为 ${protocol}，网页最低需要 ${LOCAL_WORKER_MIN_COMPATIBLE_PROTOCOL_VERSION}。`,
        fix: "更新当前制作电脑的 StoryForge，然后重新打开软件。",
      };
    }
    if (minimumBrowser > LOCAL_WORKER_PROTOCOL_VERSION) {
      return {
        compatible: false,
        code: "hub_too_old",
        message: `当前本机制作服务需要网页协议 ${minimumBrowser}，但主电脑网页仅支持 ${LOCAL_WORKER_PROTOCOL_VERSION}。`,
        fix: "请管理员先更新主电脑 StoryForge Hub，再刷新网页。",
      };
    }
    return { compatible: true, code: "compatible", message: "", fix: "" };
  }

  function setLocalWorkerIssue(value = null) {
    state.localWorkerIssue = value && typeof value === "object" ? { ...value } : null;
    if (state.bootstrapped) renderHealth();
  }

  async function connectLocalWorker({ quiet = false, force = false } = {}) {
    if (!isWebRuntime || isBrowserDemo || hasDesktopBridge() || state.webCapabilities?.client_local) return null;
    if (!force && state.localWorker?.sessionToken) return state.localWorker;
    if (webRuntime.workerConnecting) return webRuntime.workerConnecting;
    webRuntime.workerConnecting = (async () => {
      const candidates = [];
      const incompatible = [];
      const unavailable = [];
      for (const port of LOCAL_WORKER_PORTS) {
        const baseUrl = `http://127.0.0.1:${port}`;
        try {
          const { result } = await localWorkerFetch(baseUrl, "/worker/api/health", {
            method: "GET",
            headers: { Accept: "application/json" },
          }, 420);
          if (result?.ok && result.data?.service === "storyforge-local-worker") {
            const compatibility = localWorkerCompatibility(result.data);
            const candidate = { baseUrl, health: result.data, compatibility };
            if (!compatibility.compatible) incompatible.push(candidate);
            else if (!result.data?.ready) unavailable.push(candidate);
            else candidates.push(candidate);
          }
        } catch (_error) {
          // Probe the next fixed loopback port. No LAN scan is performed.
        }
      }
      if (!candidates.length) {
        state.localWorker = null;
        state.localWorkerSelfCheck = null;
        document.body.classList.remove("local-worker-ready");
        const newestIncompatible = incompatible.sort(
          (left, right) => Number(right.health?.started_at_unix || 0) - Number(left.health?.started_at_unix || 0),
        )[0];
        const newestUnavailable = unavailable.sort(
          (left, right) => Number(right.health?.started_at_unix || 0) - Number(left.health?.started_at_unix || 0),
        )[0];
        const unavailableIsHub = newestUnavailable?.health?.worker_role === "hub-only";
        const issue = newestIncompatible
          ? {
              code: newestIncompatible.compatibility.code,
              title: "本机制作服务版本不兼容",
              message: newestIncompatible.compatibility.message,
              fix: newestIncompatible.compatibility.fix,
              technical: `Worker ${newestIncompatible.health?.version || "未知"} · 协议 ${newestIncompatible.health?.protocol_version ?? "未提供"}`,
            }
          : newestUnavailable
            ? {
                code: unavailableIsHub ? "hub_is_not_worker" : "worker_hub_offline",
                title: unavailableIsHub ? "Hub 主机正常，但当前浏览器电脑还没有制作服务" : "本机制作服务尚未连接主电脑",
                message: unavailableIsHub
                  ? "Hub 只负责小说、口令、AI 和制作记录，不代替员工电脑渲染视频。"
                  : "本机服务已经启动，但当前电脑没有可用的主电脑连接。",
                fix: unavailableIsHub
                  ? "请在员工制作电脑安装并打开 StoryForge，本机制作服务会自动启动。"
                  : "打开 StoryForge 的“设置 → 多电脑协同”，重新连接主电脑后再自检。",
                technical: `Worker ${newestUnavailable.health?.version || "未知"} · role=${newestUnavailable.health?.worker_role || "unknown"} · ready=false`,
              }
            : {
              code: "worker_offline",
              title: "未连接本机制作服务",
              message: "网页可以管理小说和批次，但读取本机素材、配音与生成视频需要当前电脑的制作服务。",
              fix: "打开或重新启动当前电脑上的 StoryForge，然后回到网页点击重新自检。",
              technical: "在 127.0.0.1:18765–18770 未发现可用服务",
            };
        setLocalWorkerIssue(issue);
        if (!quiet) toast(`${issue.title}：${issue.fix}`, "error");
        return null;
      }
      // During an update an older process may still occupy the first discovery
      // port. Prefer the newest worker, and keep probing when a ready-looking
      // process cannot redeem a ticket for this account/device.
      candidates.sort((left, right) => Number(right.health?.started_at_unix || 0) - Number(left.health?.started_at_unix || 0));
      let discovered = null;
      let connection = null;
      let connectionError = "";
      for (const candidate of candidates) {
        const ticketResult = await webRequest("/web/api/local-worker-ticket", {
          method: "POST",
          jsonBody: {
            device_id: candidate.health.device_id,
            worker_nonce: candidate.health.worker_nonce,
          },
        });
        if (!ticketResult?.ok || !ticketResult.data?.ticket) {
          connectionError = ticketResult?.error || "Hub 无法授权这个本机执行器。";
          continue;
        }
        try {
          const attempt = await localWorkerFetch(candidate.baseUrl, "/worker/api/connect", {
            method: "POST",
            headers: { Accept: "application/json", "Content-Type": "application/json" },
            body: JSON.stringify({
              ticket: ticketResult.data.ticket,
              browser_protocol_version: LOCAL_WORKER_PROTOCOL_VERSION,
              minimum_worker_protocol_version: LOCAL_WORKER_MIN_COMPATIBLE_PROTOCOL_VERSION,
            }),
          // Redeeming the one-use Hub ticket and collecting the workstation
          // self-check are two bounded network operations.  Seven seconds was
          // shorter than the Hub client's own timeout and made a healthy but
          // busy main computer look disconnected on slower employee PCs.
          }, 30000);
          if (attempt.result?.ok && attempt.result.data?.session_token) {
            discovered = candidate;
            connection = attempt.result;
            break;
          }
          connectionError = attempt.result?.error || "本机执行器拒绝了连接。";
        } catch (error) {
          connectionError = error.message || String(error);
        }
      }
      if (!discovered || !connection?.data?.session_token) {
        throw new Error(connectionError || "网页未能连接当前制作电脑，请关闭旧版 StoryForge 后重试。");
      }
      setLocalWorkerIssue(null);
      state.localWorker = {
        baseUrl: discovered.baseUrl,
        sessionToken: String(connection.data.session_token),
        deviceId: String(connection.data.device_id || discovered.health.device_id || ""),
        deviceName: String(connection.data.device_name || discovered.health.device_name || "本机制作电脑"),
        folders: { ...(connection.data.folders || {}) },
        capabilities: [...(connection.data.capabilities || [])],
        runtime: { ...(connection.data.runtime || {}) },
      };
      state.localWorkerSelfCheck = connection.data.self_check && typeof connection.data.self_check === "object"
        ? { ...connection.data.self_check }
        : null;
      state.webDefaultFolders = { ...state.localWorker.folders };
      document.body.classList.add("local-worker-ready");
      if ($("#web-session-host")) $("#web-session-host").textContent = `${state.localWorker.deviceName} · 本机生成`;
      if (state.bootstrapped) {
        renderHealth();
        renderProviderStatus();
        if (state.productionNovel) renderProductionWorkbench();
      }
      return state.localWorker;
    })();
    try {
      return await webRuntime.workerConnecting;
    } catch (error) {
      state.localWorker = null;
      state.localWorkerSelfCheck = null;
      document.body.classList.remove("local-worker-ready");
      const message = error.message || "网页未能连接当前制作电脑。";
      setLocalWorkerIssue({
        code: "worker_connect_failed",
        title: "本机制作服务连接失败",
        message,
        fix: "确认当前电脑已登录正确账号；关闭旧版 StoryForge 后重新打开软件。",
        technical: message,
      });
      if (!quiet) toast(`${message} 请重新自检后再制作。`, "error");
      return null;
    } finally {
      webRuntime.workerConnecting = null;
    }
  }

  async function localWorkerRpc(method, args, { retry = true } = {}) {
    const worker = await connectLocalWorker({ quiet: false });
    if (!worker) {
      return { ok: false, error: "本机制作服务尚未连接，暂时不能读取素材、试听配音或生成视频。请打开或重新启动当前电脑上的 StoryForge。" };
    }
    try {
      const longRunningMethods = new Set([
        "generate_voice_candidates",
        "preview_voice_speed",
        "normalize_media_library",
      ]);
      const timeoutMs = longRunningMethods.has(method) ? 15 * 60 * 1000 : 120000;
      const { response, result } = await localWorkerFetch(worker.baseUrl, "/worker/api/rpc", {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-StoryForge-Worker-Session": worker.sessionToken,
        },
        body: JSON.stringify({ method, args }),
      }, timeoutMs);
      if (retry && [401, 403].includes(response.status)) {
        state.localWorker = null;
        await connectLocalWorker({ quiet: true, force: true });
        return localWorkerRpc(method, args, { retry: false });
      }
      if (result?.ok && state.localWorker) {
        if (method === "worker_runtime_snapshot" && result.data && typeof result.data === "object") {
          state.localWorker.runtime = { ...result.data };
        } else if (method === "worker_self_check" && result.data && typeof result.data === "object") {
          state.localWorkerSelfCheck = { ...result.data };
          if (result.data.runtime && typeof result.data.runtime === "object") {
            state.localWorker.runtime = { ...result.data.runtime };
          }
        } else if (method === "worker_profile" && result.data?.runtime) {
          state.localWorker.runtime = { ...result.data.runtime };
        }
      }
      return result;
    } catch (error) {
      return { ok: false, error: `当前制作电脑处理失败：${error.message || error}` };
    }
  }

  async function refreshLocalRuntimeCapabilities({ render = true } = {}) {
    if (!state.bootstrapped || isBrowserDemo || !state.webSession?.user) return;
    try {
      if (state.webCapabilities?.client_local || hasDesktopBridge()) {
        const result = await bridge.call("get_local_runtime_snapshot");
        if (!result?.ok || !result.data) return;
        state.system = { ...state.system, ...(result.data.system || {}) };
        if (state.settings?.providers && result.data.providers) {
          state.settings.providers = {
            ...state.settings.providers,
            ...result.data.providers,
          };
        }
      } else if (state.localWorker) {
        const result = await localWorkerRpc("worker_self_check", []);
        if (!result?.ok) {
          state.localWorker = null;
          document.body.classList.remove("local-worker-ready");
          await connectLocalWorker({ quiet: true, force: true });
        }
      } else {
        await connectLocalWorker({ quiet: true, force: true });
      }
      if (render) {
        renderHealth();
      }
    } catch (_error) {
      // A service restart can briefly refuse the refresh. The next focus or
      // timer tick retries without interrupting the employee's current form.
    }
  }

  function startRuntimeCapabilityRefresh() {
    if (isBrowserDemo || state.runtimeRefreshTimer) return;
    state.runtimeRefreshTimer = window.setInterval(
      () => refreshLocalRuntimeCapabilities({ render: true }),
      30000,
    );
  }

  function webFileAccept(kind) {
    return {
      txt: ".txt,text/plain",
      docx: ".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      summary: ".txt,.docx,text/plain,application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      cover: ".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp",
      update_package: ".zip,application/zip,application/x-zip-compressed",
    }[String(kind || "").toLowerCase()] || "*/*";
  }

  function chooseBrowserFile(kind) {
    const input = $("#web-file-picker");
    if (!input) return Promise.reject(new Error("网页文件选择器尚未就绪，请刷新页面后重试。"));
    input.accept = webFileAccept(kind);
    input.value = "";
    return new Promise((resolve) => {
      let settled = false;
      const finish = (file) => {
        if (settled) return;
        settled = true;
        window.removeEventListener("focus", onWindowFocus);
        input.removeEventListener("change", onInputChange);
        resolve(file || null);
      };
      const onWindowFocus = () => window.setTimeout(() => finish(input.files?.[0]), 350);
      const onInputChange = () => finish(input.files?.[0]);
      input.addEventListener("change", onInputChange, { once: true });
      window.addEventListener("focus", onWindowFocus, { once: true });
      input.click();
    });
  }

  function webAssetUrl(source, purpose = "media") {
    const value = String(source || "").trim();
    if (!value) return "";
    if (!isWebRuntime || hasDesktopBridge() || isBrowserDemo) return value;
    const uploaded = state.webUploadAssets.get(value);
    if (uploaded) return String(purpose === "download" ? uploaded.download_url || uploaded.media_url : uploaded.media_url || uploaded.download_url || "");
    if (value.startsWith("/worker/api/") && state.localWorker?.baseUrl) {
      return `${state.localWorker.baseUrl}${value}`;
    }
    if (/^(data:|blob:|https?:)/i.test(value) || value.startsWith("/web/api/")) {
      if (purpose === "download" && value.startsWith("/web/api/media?")) return value.replace("/web/api/media?", "/web/api/download?");
      return value;
    }
    // A normal browser cannot dereference D:\..., file:// or hub:// paths. The
    // authenticated backend rewrites shareable assets to /web/api/media?ref=.
    return "";
  }

  function webAssetDisplayName(source) {
    const value = String(source || "").trim();
    return state.webUploadAssets.get(value)?.name || pathLeaf(value);
  }

  function artifactMediaSource(artifact, purpose = "media") {
    const source = artifact?.[purpose === "download" ? "download_uri" : "media_uri"]
      || artifact?.web_uri
      || artifact?.uri
      || "";
    return webAssetUrl(source, purpose);
  }

  async function uploadBrowserFile(kind) {
    const file = await chooseBrowserFile(kind);
    if (!file) return { ref: "", name: "", media_url: "", download_url: "" };
    const formData = new FormData();
    formData.append("file", file, file.name);
    formData.append("kind", String(kind || "file"));
    const result = await webRequest(`/web/api/upload?kind=${encodeURIComponent(kind)}`, { method: "POST", formData });
    if (!result?.ok) throw new Error(result?.error || "文件上传失败，请重试。");
    const data = typeof result.data === "string" ? { ref: result.data } : (result.data || {});
    const ref = String(data.ref || data.file_ref || data.upload_ref || "");
    if (!ref) throw new Error("文件已经上传，但服务未返回文件引用。请升级 StoryForge Hub。");
    const localPreview = String(file.type || "").startsWith("image/") ? URL.createObjectURL(file) : "";
    const asset = {
      ref,
      name: String(data.name || data.file_name || file.name),
      media_url: String(data.media_url || data.url || localPreview || ""),
      download_url: String(data.download_url || ""),
    };
    state.webUploadAssets.set(ref, asset);
    return asset;
  }

  function requireWebLogin(message = "") {
    if ((!isWebRuntime && !hasDesktopBridge()) || isBrowserDemo) return;
    stopPolling();
    state.bootstrapped = false;
    state.webSession = null;
    state.webCapabilities = {};
    state.localWorker = null;
    state.localWorkerIssue = null;
    state.localWorkerSelfCheck = null;
    webRuntime.csrfToken = "";
    document.body.classList.remove("local-worker-ready");
    document.body.classList.add("web-runtime", "web-auth-required");
    $("#web-login")?.classList.remove("is-hidden");
    $("#web-session-bar")?.classList.add("is-hidden");
    if ($("#web-login-status")) $("#web-login-status").textContent = message;
    window.setTimeout(() => $("#web-login-username")?.focus(), 40);
  }

  function applyWebSession(session) {
    rememberWebSessionData(session);
    document.body.classList.add("web-runtime");
    document.body.classList.remove("web-auth-required");
    $("#web-login")?.classList.add("is-hidden");
    $("#web-session-bar")?.classList.remove("is-hidden");
    const user = state.webSession?.user || {};
    if ($("#web-session-name")) $("#web-session-name").textContent = user.display_name || user.username || "成员";
    if ($("#web-session-host")) $("#web-session-host").textContent = state.webSession?.host_name || window.location.host;
    const passwordButton = $("#web-change-password");
    if (passwordButton) passwordButton.textContent = state.webSession?.password_configured === false ? "设置登录密码" : "修改密码";
    if (passwordButton) passwordButton.classList.toggle("is-hidden", state.webCapabilities?.password_change === false);
    const logoutButton = $("#web-logout");
    if (logoutButton) logoutButton.classList.toggle("is-hidden", state.webCapabilities?.logout === false);
    document.body.classList.toggle("client-local-web", Boolean(state.webCapabilities?.client_local));
    document.body.classList.toggle("is-employee-session", normalizeSoftwareRole(user.role) === "producer");
    document.body.classList.toggle("is-admin-session", normalizeSoftwareRole(user.role) === "admin");
    applyWebCapabilityHints();
    applyProviderAccessMode();
  }

  function applyWebCapabilityHints(root = document) {
    if (!state.webSession?.user) return;
    const localMediaReady = hasLocalMediaRuntime();
    const permissions = new Set(state.webSession?.permissions || []);
    const permissionClasses = {
      "web-can-admin": ["hub.manage", "users.manage", "permissions.manage"],
      "web-can-library-edit": ["library.edit"],
      "web-can-platform-manage": ["platforms.manage", "promo_codes.manage"],
      "web-can-publishing-manage": ["publishing_accounts.manage"],
      "web-can-global-queue": ["drafts.create", "drafts.manage_all", "hub.manage"],
    };
    Object.entries(permissionClasses).forEach(([className, required]) => {
      document.body.classList.toggle(className, required.some((permission) => permissions.has(permission)));
    });
    document.body.classList.toggle(
      "web-can-local-update",
      Boolean(state.webCapabilities?.client_local)
        && (permissions.has("updates.manage_own") || permissions.has("hub.manage")),
    );
    renderSettingsAccess();
    $$('[data-draft-folder]', root).forEach((button) => {
      button.classList.remove("web-host-only");
      button.textContent = localMediaReady ? "选择本机文件夹" : "重新连接本机服务";
      button.title = localMediaReady
        ? "文件夹只保存在当前制作电脑，不会同步到 Hub 主机。"
        : "请打开或重新启动当前电脑上的 StoryForge，再选择本机素材和输出文件夹。";
    });
    $$('[data-output-folder]', root).forEach((button) => {
      button.textContent = localMediaReady ? "打开本机输出" : "重新连接本机服务";
      button.title = "成片只保存在执行任务的员工电脑。";
    });
  }

  function renderSettingsAccess() {
    const employee = isEmployeeSession();
    const navCopy = $("#settings-nav-copy");
    const title = $("#settings-page-title");
    const copy = $("#settings-page-copy");
    const styleCopy = $("#settings-style-entry-copy");
    const localKicker = $("#settings-local-entry-kicker");
    const localTitle = $("#settings-local-entry-title");
    const localCopy = $("#settings-local-entry-copy");
    if (navCopy) navCopy.textContent = employee ? "个人方案 · 本机维护" : "服务 · 团队 · 多电脑";
    if (title) title.textContent = employee ? "只设置自己的制作效果和当前电脑" : "按工作顺序设置，不用记技术名词";
    if (copy) copy.textContent = employee
      ? "个人方案不会覆盖团队默认；小说、口令、平台、成员与 Hub 仍由管理员维护。"
      : "先完成首次使用三步，其他选项按需调整；已经保存的设置不会丢失。";
    if (styleCopy) styleCopy.textContent = employee
      ? "预览并编辑字幕、简介卡、口令和封面结尾，保存为自己的制作方案。"
      : "设置新任务的初始语速、音乐、字幕和安全区。";
    if (localKicker) localKicker.textContent = employee ? "THIS COMPUTER" : "LOCAL & CLOUD";
    if (localTitle) localTitle.textContent = employee ? "本机维护与软件更新" : "服务与本机运行环境";
    if (localCopy) localCopy.textContent = employee
      ? "检查 FFmpeg、配音和编码器状态，并更新当前这台制作电脑。"
      : "配置润色、配音、额度和硬件编码器。";
  }

  async function initializeWebRuntime() {
    document.body.classList.add("web-runtime", "web-auth-required");
    if (isBrowserDemo) {
      applyWebSession({
        user: { username: "preview", display_name: "浏览器演示", role: "admin" },
        permissions: softwarePermissionCatalog.map(([permission]) => permission),
        password_configured: true,
        csrf_token: "demo",
      });
      await bootstrap();
      return;
    }
    try {
      const result = await webRequest("/web/api/session", { allowUnauthorized: true });
      if (result?.ok && result.data?.user) {
        applyWebSession(result.data);
        await connectLocalWorker({ quiet: true });
        await bootstrap();
      } else {
        requireWebLogin("");
      }
    } catch (_error) {
      requireWebLogin("无法连接 StoryForge Hub。请确认主电脑服务正在运行，然后刷新页面。");
    }
  }

  function initializeDesktopRuntime() {
    if (webRuntime.desktopInitialized) return Promise.resolve();
    if (webRuntime.desktopInitializing) return webRuntime.desktopInitializing;
    webRuntime.desktopInitializing = (async () => {
      document.body.classList.add("web-runtime", "web-auth-required", "desktop-auth-runtime");
      if ($("#web-login-title")) $("#web-login-title").textContent = "登录 StoryForge 制作台";
      const loginCopy = document.querySelector(".web-login-copy > p:last-child");
      if (loginCopy) loginCopy.textContent = "使用管理员分配的账号和 8 位密码登录。素材、配音、渲染与成片仍只保存在当前电脑。";
      const sessionMode = document.querySelector("#web-session-bar .web-session-mode b");
      if (sessionMode) sessionMode.textContent = "桌面制作台";
      try {
        const result = await bridge.call("desktop_session_status");
        if (!result?.ok) throw new Error(result?.error || "无法验证本机登录状态。");
        if (result?.ok && result.data?.authenticated && result.data?.user) {
          applyWebSession(result.data);
          await bootstrap();
        } else {
          requireWebLogin("");
        }
        webRuntime.desktopInitialized = true;
      } catch (_error) {
        webRuntime.desktopInitializing = null;
        requireWebLogin("无法验证本机登录状态，请重新登录。");
      }
    })();
    return webRuntime.desktopInitializing;
  }

  const bridge = {
    async call(method, ...args) {
      const api = hasDesktopBridge() ? window.pywebview.api : null;
      if (api && typeof api[method] === "function") {
        return api[method](...args);
      }
      if (api && typeof api.desktop_rpc === "function") {
        return api.desktop_rpc(method, args);
      }
      if (api) {
        return {
          ok: false,
          error: `当前桌面后端尚未提供 ${method}。请升级 StoryForge 后端后重试；现有制作队列不受影响。`,
        };
      }
      if (isWebRuntime && !isBrowserDemo) {
        if (method === "choose_file") {
          try {
            const asset = await uploadBrowserFile(args[0]);
            return { ok: true, data: asset.ref };
          } catch (error) {
            return { ok: false, error: error.message || "文件上传失败。" };
          }
        }
        if (localWorkerRpcMethods.has(method)) {
          // A browser opened from this workstation's loopback StoryForge URL
          // already shares the local API process. Sending it through worker
          // discovery would deliberately return null and make the web UI look
          // read-only. Hub-hosted pages still use the isolated localhost worker.
          if (state.webCapabilities?.client_local) return webRpc(method, args);
          return localWorkerRpc(method, args);
        }
        return webRpc(method, args);
      }
      if (isBrowserDemo) {
        if (method === "get_bootstrap") return { ok: true, data: structuredClone(browserMockState) };
        if (method === "set_local_tts_provider") {
          const provider = String(args[0] || "").toLocaleLowerCase().replaceAll("-", "_");
          if (!new Set(["edge_tts", "local_kokoro"]).has(provider)) return { ok: false, error: "只能切换免费本机配音服务。" };
          browserMockState.settings.providers.tts_provider = provider;
          return { ok: true, data: { tts_provider: provider, edge_tts_runtime_ready: true, embedded_kokoro_ready: true } };
        }
        if (method === "get_local_self_check") {
          return { ok: true, data: { ready: true, status: "ready", summary: "当前制作电脑已就绪", checked_at_unix: Date.now() / 1000, runtime: { app_version: "demo", worker_protocol_version: LOCAL_WORKER_PROTOCOL_VERSION, ffmpeg_ready: true, encoders: ["h264_nvenc", "libx264"], recommended_encoder: "h264_nvenc", edge_tts_runtime_ready: true, embedded_kokoro_ready: true }, checks: [{ key: "ffmpeg", label: "FFmpeg", status: "ok", summary: "FFmpeg 可用", fix: "", technical: { ready: true } }, { key: "encoder", label: "H.264 编码器", status: "ok", summary: "h264_nvenc / libx264", fix: "", technical: { encoders: ["h264_nvenc", "libx264"] } }] } };
        }
        if (method === "get_production_presets") {
          return { ok: true, data: structuredClone(browserMockState.production_presets) };
        }
        if (method === "save_production_preset") {
          const value = structuredClone(args[0] || {});
          value.id ||= `custom_${Date.now()}`;
          const existing = browserMockState.production_presets.items.find((item) => item.id === value.id);
          value.curated = Boolean(existing?.curated);
          value.scope = value.curated ? "curated" : "personal";
          value.owner_user_id = String(existing?.owner_user_id || state.webSession?.user?.id || "demo-user");
          value.owner_display_name = String(existing?.owner_display_name || state.webSession?.user?.display_name || state.webSession?.user?.username || "演示账号");
          value.editable = true;
          value.deletable = true;
          value.owned_by_current_user = !value.curated;
          const index = browserMockState.production_presets.items.findIndex((item) => item.id === value.id);
          if (index >= 0) browserMockState.production_presets.items[index] = value;
          else browserMockState.production_presets.items.push(value);
          browserMockState.production_presets.total = browserMockState.production_presets.items.length;
          return { ok: true, data: structuredClone(value) };
        }
        if (method === "delete_production_preset") {
          const id = String(args[0] || "");
          const target = browserMockState.production_presets.items.find((item) => item.id === id);
          if (!target?.deletable) return { ok: false, error: "当前方案不能删除或恢复。" };
          if (target?.curated) {
            target.deletable = false;
            return { ok: true, data: { id, deleted: true, reset_to_curated: true } };
          }
          browserMockState.production_presets.items = browserMockState.production_presets.items.filter((item) => item.id !== id);
          browserMockState.production_presets.total = browserMockState.production_presets.items.length;
          return { ok: true, data: { id, deleted: true, reset_to_curated: false } };
        }
        if (method === "get_library_bootstrap") {
          return { ok: true, data: structuredClone(browserMockLibrary) };
        }
        if (method === "get_record_artifacts") {
          const recordId = String(args[0] || "");
          const record = browserMockLibrary.production_records.find((item) => item.id === recordId);
          if (!record) return { ok: false, error: "没有找到这条生产记录。" };
          const artifacts = Number(record.artifact_count || 0) > 0 ? [
            {
              id: "artifact-preview-so2",
              record_id: record.id,
              kind: "sample",
              device_id: "StoryForge-Hub",
              local_path: "hub://attachments/record-complete-so2/preview.mp4",
              cached_path: "D:\\StoryForgeHub\\cache\\preview.mp4",
              uri: MOCK_PREVIEW_VIDEO_URI,
              mime_type: "video/mp4",
              size_bytes: 2483200,
              duration_seconds: 30,
              available: true,
              metadata: { source: "hub-cache" },
            },
            {
              id: "artifact-narration-so2",
              record_id: record.id,
              kind: "preview_narration",
              device_id: "Studio-PC-01",
              local_path: "D:\\StoryForgeMedia\\narration\\preview.wav",
              cached_path: "D:\\StoryForgeMedia\\narration\\preview.wav",
              uri: MOCK_NARRATION_AUDIO_URI,
              mime_type: "audio/wav",
              size_bytes: 768000,
              duration_seconds: 30,
              available: true,
              metadata: {},
            },
            {
              id: "artifact-alignment-so2",
              record_id: record.id,
              kind: "preview_alignment",
              device_id: "Studio-PC-01",
              local_path: "D:\\StoryForgeMedia\\alignment\\preview.ass",
              cached_path: "D:\\StoryForgeMedia\\alignment\\preview.ass",
              uri: "data:text/plain;charset=utf-8,StoryForge%2015s%20alignment%20proof",
              mime_type: "text/x-ssa",
              size_bytes: 18420,
              duration_seconds: 30,
              available: true,
              metadata: {},
            },
          ] : [];
          return { ok: true, data: structuredClone({ record, artifacts }) };
        }
        if (method === "choose_folder") {
          const value = window.prompt("输入本地文件夹路径", "") || "";
          return { ok: true, data: value };
        }
        if (method === "choose_file") {
          const kind = String(args[0] || "txt").toLowerCase();
          const names = {
            txt: "D:\\StoryForgeMedia\\novels\\The Imported Secret.txt",
            docx: "D:\\StoryForgeMedia\\novels\\The Imported Secret.docx",
            summary: "D:\\StoryForgeMedia\\synopsis\\Story Summary.docx",
            cover: "D:\\StoryForgeMedia\\covers\\Selected Cover.jpg",
            audio: "D:\\StoryForgeMedia\\audio\\Existing Narration.mp3",
            update_package: "D:\\StoryForgeUpdates\\StoryForge-2.4.0.zip",
          };
          return { ok: true, data: names[kind] || names.txt };
        }
        if (method === "read_text_document") {
          return { ok: true, data: { text: "On the night she planned to forgive him, a hidden photograph revealed the marriage was built on someone else's name. She leaves before dawn—but the truth follows her.", file_path: String(args[0] || "") } };
        }
        if (method === "import_novel_text" || method === "import_novel_file") {
          const payload = args[0] || {};
          const title = String(payload.title || "").trim();
          const text = String(payload.text || "").trim();
          const filePath = String(payload.file_path || "").trim();
          if (!title) return { ok: false, error: "请填写小说标题。" };
          if (method === "import_novel_text" && !text) return { ok: false, error: "请先粘贴小说正文。" };
          if (method === "import_novel_file" && !filePath) return { ok: false, error: "请先选择 TXT 或 DOCX 文件。" };
          const targetNovel = payload.novel_id
            ? browserMockLibrary.novels.find((item) => item.id === payload.novel_id)
            : null;
          if (payload.novel_id && !targetNovel) return { ok: false, error: "没有找到要更新的小说。" };
          const detectedLanguage = mockLanguageDetection(payload, `${title} ${text} ${payload.synopsis || ""}`);
          if (targetNovel) {
            targetNovel.title = title;
            targetNovel.synopsis = String(payload.synopsis || targetNovel.synopsis || "").trim();
            targetNovel.language = detectedLanguage.code;
            targetNovel.language_detection = detectedLanguage;
            targetNovel.source_type = method === "import_novel_text" ? "paste" : String(payload.source_type || "txt");
            targetNovel.source_chapters = Math.max(Number(targetNovel.source_chapters || 0), targetNovel.episodes?.length || 1);
            targetNovel.revision_count = Number(targetNovel.revision_count || 1) + 1;
            targetNovel.updated_at = new Date().toISOString();
            return { ok: true, data: structuredClone(targetNovel) };
          }
          const duplicate = browserMockLibrary.novels.find(
            (item) => item.title.trim().toLocaleLowerCase() === title.toLocaleLowerCase(),
          );
          if (duplicate) return { ok: false, error: `小说“${title}”已在库中，请直接打开原记录。` };
          const id = `novel-${Date.now()}`;
          const sourceType = method === "import_novel_text" ? "paste" : String(payload.source_type || "txt");
          const episodeCount = sourceType === "paste" ? 2 : 3;
          const novel = {
            id,
            title,
            cover_path: "",
            cover_uri: "",
            language: detectedLanguage.code,
            language_detection: detectedLanguage,
            synopsis: String(payload.synopsis || "").trim() || "尚未填写故事简介。",
            tags: ["New import"],
            cover_tone: ["cobalt", "ember", "violet"][browserMockLibrary.novels.length % 3],
            source_type: sourceType,
            source_chapters: episodeCount,
            estimated_duration_seconds: episodeCount * 390,
            default_voice: detectedLanguage.code === "en" ? "American female · 待试听" : `${detectedLanguage.display_name}女声 · 待试听`,
            locked_voice_provider: "",
            locked_voice_id: "",
            voice_candidates: [],
            progress: { completed: 0, total: episodeCount },
            platform_bindings: [],
            episodes: Array.from({ length: episodeCount }, (_, index) => ({
              id: `${id}-e${index + 1}`,
              number: index + 1,
              title: `自动分集 ${index + 1}`,
              source_label: `源章节 ${index + 1}`,
              duration_seconds: 360 + index * 24,
              status: "ready",
            })),
            materials: [],
            draft: { id: `draft-${id}`, platform_id: "", promo_code_id: "", publishing_account_id: "", episode_ids: [`${id}-e1`], variant_count: 1, approvals: { main: "pending", variants: {} }, video_folder: "", music_folder: "", output_folder: "" },
            updated_at: new Date().toISOString(),
          };
          browserMockLibrary.novels.unshift(novel);
          return { ok: true, data: structuredClone(novel) };
        }
        if (method === "get_novel") {
          const novel = browserMockLibrary.novels.find((item) => item.id === args[0]);
          return novel ? { ok: true, data: structuredClone(novel) } : { ok: false, error: "没有找到这部小说。" };
        }
        if (method === "classify_novel") {
          const novel = browserMockLibrary.novels.find((item) => item.id === args[0]);
          if (!novel) return { ok: false, error: "没有找到这部小说。" };
          const text = [novel.title, novel.synopsis, ...(novel.tags || [])].join(" ").toLocaleLowerCase();
          const mood = /revenge|betray|divorc|payback|爽文|复仇/.test(text)
            ? "revenge"
            : /funeral|death|grief|sad|悲伤/.test(text)
              ? "sad"
              : /romance|wedding|love|bride|浪漫/.test(text)
                ? "romance"
                : "suspense";
          novel.story_classification = {
            mood,
            label: storyMoodCatalog[mood].label,
            source: "ai",
            provider: "browser-preview-ai",
            model: "demo",
            content_hash: novel.content_hash || "mock-content",
            classified_at: new Date().toISOString(),
            warning: "",
          };
          return { ok: true, data: structuredClone({ classification: novel.story_classification, novel, cached: false }) };
        }
        if (method === "save_novel") {
          const payload = args[0] || {};
          const index = browserMockLibrary.novels.findIndex((item) => item.id === payload.id);
          if (index < 0) return { ok: false, error: "没有找到要保存的小说。" };
          const current = browserMockLibrary.novels[index];
          const nextPayload = { ...payload };
          delete nextPayload.redetect_language;
          if (payload.redetect_language) {
            const detection = mockLanguageDetection({}, `${payload.title || current.title} ${payload.synopsis || current.synopsis}`);
            nextPayload.language = detection.code;
            nextPayload.language_detection = detection;
          } else if (payload.language) {
            const detection = mockLanguageDetection({ language: payload.language }, "");
            nextPayload.language = detection.code;
            nextPayload.language_detection = detection;
          }
          browserMockLibrary.novels[index] = { ...current, ...nextPayload, updated_at: new Date().toISOString() };
          if (payload.cover_path) browserMockLibrary.novels[index].cover_uri = mockCoverDataUri(browserMockLibrary.novels[index].title, "#e85e50", "#14213d");
          return { ok: true, data: structuredClone(browserMockLibrary.novels[index]) };
        }
        if (method === "save_novel_binding") {
          const payload = args[0] || {};
          const novel = browserMockLibrary.novels.find((item) => item.id === payload.novel_id);
          if (!novel) return { ok: false, error: "没有找到要绑定的小说。" };
          if (!payload.platform_id) return { ok: false, error: "请选择平台。" };
          let binding = novel.platform_bindings.find((item) => item.platform_id === payload.platform_id);
          if (!binding) {
            binding = { platform_id: payload.platform_id, codes: [] };
            novel.platform_bindings.push(binding);
          }
          novel.draft.platform_id = payload.platform_id;
          novel.draft.promo_code_id = "";
          return { ok: true, data: structuredClone(novel) };
        }
        if (method === "add_promo_code") {
          const payload = args[0] || {};
          const novel = browserMockLibrary.novels.find((item) => item.id === payload.novel_id);
          const binding = novel?.platform_bindings.find((item) => item.platform_id === payload.platform_id);
          const code = String(payload.code || "").trim().toUpperCase();
          if (!binding) return { ok: false, error: "请先绑定平台。" };
          if (!/^[A-Z0-9]+$/.test(code)) return { ok: false, error: "口令只允许英文字母和数字。" };
          if (binding.codes.length >= 5) return { ok: false, error: "该平台已达到5个口令上限。" };
          if (binding.codes.some((item) => item.value.toUpperCase() === code)) return { ok: false, error: "这个口令已经存在。" };
          const promo = { id: `code-${Date.now()}`, value: code, active: true, use_count: 0 };
          binding.codes.push(promo);
          return { ok: true, data: structuredClone({ novel, promo_code: promo }) };
        }
        if (method === "update_promo_code") {
          const payload = args[0] || {};
          const novel = browserMockLibrary.novels.find((item) => item.id === payload.novel_id);
          const binding = novel?.platform_bindings.find((item) => item.platform_id === payload.platform_id);
          const promo = binding?.codes.find((item) => item.id === payload.promo_code_id);
          if (!promo) return { ok: false, error: "没有找到这个口令。" };
          promo.active = Boolean(payload.active);
          return { ok: true, data: structuredClone({ novel, promo_code: promo }) };
        }
        if (method === "save_publishing_account") {
          const payload = args[0] || {};
          const name = String(payload.name || "").trim();
          if (!payload.platform_id || !name) return { ok: false, error: "请选择平台并填写发布账号名称。" };
          const index = browserMockLibrary.publishing_accounts.findIndex((item) => item.id === payload.id);
          const current = index >= 0 ? browserMockLibrary.publishing_accounts[index] : null;
          if (current && Number(payload.expected_version || 0) !== Number(current.row_version || 0)) {
            return { ok: false, error: "账号资料已被其他电脑更新，请重新打开后再保存。" };
          }
          const account = {
            ...(current || {}),
            id: index >= 0 ? payload.id : `account-${Date.now()}`,
            platform_id: payload.platform_id,
            name,
            handle: String(payload.handle || "").trim(),
            region: String(payload.region || "").trim(),
            positioning: String(payload.positioning || "").trim(),
            notes: String(payload.notes || "").trim(),
            active: payload.active !== false,
            row_version: Number(current?.row_version || 0) + 1,
          };
          if (index >= 0) browserMockLibrary.publishing_accounts[index] = account;
          else browserMockLibrary.publishing_accounts.push(account);
          return { ok: true, data: structuredClone(account) };
        }
        if (method === "generate_intro_card_copy") {
          const novel = browserMockLibrary.novels.find((item) => item.id === args[0]);
          if (!novel) return { ok: false, error: "没有找到这部小说。" };
          const selected = new Set(Array.isArray(args[1]) ? args[1] : []);
          const selectedEpisodes = (novel.episodes || []).filter((item) => selected.has(item.id));
          const fallbackEpisode = novel.episodes?.[0];
          const draft = {
            episode_ids: selectedEpisodes.length
              ? selectedEpisodes.map((item) => item.id)
              : (fallbackEpisode ? [fallbackEpisode.id] : []),
          };
          return {
            ok: true,
            data: {
              text: storyPreviewText(novel, draft),
              source: "novel_synopsis_ai",
            },
          };
        }
        if (method === "generate_voice_candidates") {
          const novel = browserMockLibrary.novels.find((item) => item.id === args[0]);
          if (!novel) return { ok: false, error: "没有找到这部小说。" };
          const mood = String(args[1] || "suspense");
          const excerpt = String(
            novel.synopsis
            || novel.episodes?.[0]?.text
            || "Add a synopsis or episode before previewing this voice.",
          ).slice(0, 180);
          const languageInfo = novelLanguageInfo(novel);
          const presets = languageInfo.key === "en"
            ? [
                ["warm", "Ava · Warm American", "ava-warm"],
                ["intimate", "Maya · Intimate American", "maya-intimate"],
                ["confident", "Sloane · Confident American", "sloane-confident"],
              ]
            : [
                ["warm", `${languageInfo.label} · 温暖女声`, `${languageInfo.key}-warm`],
                ["intimate", `${languageInfo.label} · 亲密女声`, `${languageInfo.key}-intimate`],
                ["confident", `${languageInfo.label} · 坚定女声`, `${languageInfo.key}-confident`],
              ];
          novel.voice_candidates = presets.map(([profile, label, voiceId]) => ({
            profile,
            label: `${label} · ${mood}`,
            provider: "browser_speech",
            voice_id: voiceId,
            audio_path: "",
            audio_uri: `mock://speech/${voiceId}`,
            duration_seconds: 12,
            excerpt,
          }));
          return { ok: true, data: structuredClone({ candidates: novel.voice_candidates, novel }) };
        }
        if (method === "lock_novel_voice") {
          const novel = browserMockLibrary.novels.find((item) => item.id === args[0]);
          const candidate = args[1] || {};
          if (!novel) return { ok: false, error: "没有找到这部小说。" };
          if (!candidate.provider || !candidate.voice_id) return { ok: false, error: "请选择一个有效的女声候选。" };
          novel.locked_voice_provider = candidate.provider;
          novel.locked_voice_id = candidate.voice_id;
          novel.default_voice = candidate.label || candidate.voice_id;
          novel.locked_voice_profile = candidate.profile || "";
          return { ok: true, data: structuredClone(novel) };
        }
        if (method === "save_production_draft") {
          const payload = args[0] || {};
          const novel = browserMockLibrary.novels.find((item) => item.id === payload.novel_id);
          if (!novel) return { ok: false, error: "没有找到这部小说。" };
          if (!payload.platform_id || !payload.promo_code_id) return { ok: false, error: "请选择平台和本批口令。" };
          if (!Array.isArray(payload.episode_ids) || !payload.episode_ids.length) return { ok: false, error: "至少选择一集。" };
          const binding = novel.platform_bindings.find((item) => item.platform_id === payload.platform_id);
          const code = binding?.codes.find((item) => item.id === payload.promo_code_id && item.active);
          if (!code) return { ok: false, error: "本批口令不属于所选平台，或已经停用。" };
          const draftId = String(payload.id || "").trim()
            || `draft-${novel.id}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
          novel.draft = { ...structuredClone(payload), id: draftId, row_version: Number(payload.row_version || 0) + 1 };
          novel.updated_at = new Date().toISOString();
          const account = browserMockLibrary.publishing_accounts.find((item) => item.id === payload.publishing_account_id);
          const record = {
            id: `record-${draftId}`,
            batch_id: draftId,
            draft_id: draftId,
            novel_id: novel.id,
            title: novel.title,
            episode_label: `${payload.episode_ids.length}集连续正文`,
            creative_line: Number(payload.variant_count || 1),
            status: "draft",
            platform_id: payload.platform_id,
            promo_code: code?.value || "",
            publishing_account_name: account?.name || "待分配",
            stage_label: "制作方案已保存",
            progress: 0,
            error: "",
            output_folder: payload.output_folder || "",
            materials: novel.materials.map((item) => ({ name: item.name, usage_count: item.usage_count })),
            created_at: new Date().toISOString(),
          };
          const recordIndex = browserMockLibrary.production_records.findIndex((item) => item.id === record.id);
          if (recordIndex >= 0) browserMockLibrary.production_records[recordIndex] = record;
          else browserMockLibrary.production_records.unshift(record);
          return { ok: true, data: structuredClone({ novel, draft: novel.draft, record }) };
        }
        if (method === "queue_production_draft") {
          const payload = args[0] || {};
          const novel = browserMockLibrary.novels.find((item) => item.id === payload.novel_id || item.draft?.id === payload.draft_id);
          const draft = novel?.draft;
          if (!novel || !draft || !payload.draft_id) return { ok: false, error: "请先保存生产草稿。" };
          const outputMode = normalizedProductionOutputMode(draft.production_settings?.output_mode);
          if (outputMode !== "reuse_audio" && (!draft.voice?.provider || !draft.voice?.voice_id)) {
            return { ok: false, error: "请试听并选择本批女声。" };
          }
          if (outputMode === "reuse_audio" && !String(draft.source_narration_audio || payload.source_narration_audio || "").trim()) {
            return { ok: false, error: "请选择已有配音。" };
          }
          const related = browserMockState.jobs.some((job) => job.production_draft_id === draft.id && !terminalStatuses.has(job.status));
          if (related) return { ok: false, error: "该制作方案已经在队列中，请先处理现有任务。" };
          const binding = novel.platform_bindings.find((item) => item.platform_id === draft.platform_id);
          const code = binding?.codes.find((item) => item.id === draft.promo_code_id);
          const selected = (novel.episodes || []).filter((episode) => draft.episode_ids.includes(episode.id));
          if (!selected.length) return { ok: false, error: "至少选择一个分集。" };
          const requestedCount = outputMode === "audio_only"
            ? 1
            : Math.trunc(Number(draft.target_video_count || draft.variant_count || 1));
          const targetCount = Number.isFinite(requestedCount) ? Math.max(1, requestedCount) : 1;
          const firstEpisode = selected[0];
          const lastEpisode = selected[selected.length - 1];
          const selectedNumbers = selected.map((episode, index) => Math.max(1, Number(episode.number) || index + 1));
          const contiguousSelection = selectedNumbers.every((number, index) => (
            index === 0 || number === selectedNumbers[index - 1] + 1
          ));
          const episodeLabel = selectedNumbers.length === 1
            ? `E${String(selectedNumbers[0]).padStart(3, "0")}`
            : contiguousSelection
              ? `E${String(selectedNumbers[0]).padStart(3, "0")}-E${String(selectedNumbers[selectedNumbers.length - 1]).padStart(3, "0")}`
              : selectedNumbers.map((number) => `E${String(number).padStart(3, "0")}`).join("_");
          const jobs = Array.from({ length: targetCount }, (_, variantOffset) => {
            return {
              id: `mock-${draft.id}-${firstEpisode.id}-${variantOffset + 1}-${Date.now()}`,
              batch_id: draft.id,
              production_draft_id: draft.id,
              novel_id: novel.id,
              episode_id: firstEpisode.id,
              episode_ids: selected.map((episode) => episode.id),
              episode_number: firstEpisode.number,
              episode_label: episodeLabel,
              is_final_episode: lastEpisode.id === novel.episodes?.[novel.episodes.length - 1]?.id,
              variant_index: variantOffset + 1,
              variant_count: targetCount,
              platform_id: draft.platform_id,
              title: novel.title,
              code: code?.value || "",
              source_file: `${novel.title} · ${episodeLabel}`,
              video_folder: draft.video_folder,
              music_folder: draft.music_folder,
              output_folder: draft.output_folder,
              source_narration_audio: String(draft.source_narration_audio || payload.source_narration_audio || ""),
              output_file: "",
              status: "queued",
              progress: 0,
              stage_label: "等待处理",
              job_kind: "full",
              preview_file: "",
              preview_uri: "",
              preview_approved: true,
              locked_voice_provider: draft.voice?.provider || "",
              locked_voice_id: draft.voice?.voice_id || "",
              settings_snapshot: structuredClone(draft.production_settings || {}),
              created_at: new Date().toISOString(),
            };
          });
          browserMockState.jobs.push(...jobs);
          return { ok: true, data: structuredClone({ draft, jobs, preview_job_id: "", total_videos: jobs.length }) };
        }
        if (method === "approve_preview") {
          const job = browserMockState.jobs.find((item) => item.id === args[0]);
          if (!job || job.status !== "awaiting_approval") return { ok: false, error: "当前历史任务不支持此兼容操作。" };
          const siblings = browserMockState.jobs.filter((item) => item.production_draft_id === job.production_draft_id && ["awaiting_approval", "waiting_preview"].includes(item.status));
          siblings.forEach((item) => Object.assign(item, { status: "queued", progress: 0, job_kind: "full", preview_approved: true, stage_label: "历史任务已继续，等待完整制作" }));
          if (siblings[0]) Object.assign(siblings[0], { status: "narrating", progress: 0.12, stage_label: "生成旁白" });
          return { ok: true, data: structuredClone(job) };
        }
        if (method === "regenerate_preview") {
          const job = browserMockState.jobs.find((item) => item.id === args[0]);
          if (!job || !["awaiting_approval", "failed", "cancelled", "interrupted"].includes(job.status)) return { ok: false, error: "当前历史任务不能执行此兼容操作。" };
          const novel = browserMockLibrary.novels.find((item) => item.draft?.id === job.production_draft_id);
          const draft = novel?.draft;
          browserMockState.jobs.filter((item) => item.production_draft_id === job.production_draft_id).forEach((item) => {
            if (draft) Object.assign(item, {
              locked_voice_provider: draft.voice?.provider || item.locked_voice_provider,
              locked_voice_id: draft.voice?.voice_id || item.locked_voice_id,
              settings_snapshot: structuredClone(draft.production_settings || item.settings_snapshot || {}),
              video_folder: draft.video_folder || item.video_folder,
              music_folder: draft.music_folder || item.music_folder,
              output_folder: draft.output_folder || item.output_folder,
            });
            if (item.id === job.id) Object.assign(item, { status: "previewing", progress: 0.05, job_kind: "preview", preview_uri: "", preview_file: "", preview_approved: false, stage_label: "正在迁移历史预览任务" });
            else if (!terminalStatuses.has(item.status)) Object.assign(item, { status: "waiting_preview", progress: 0, stage_label: "等待历史任务迁移" });
          });
          return { ok: true, data: structuredClone(job) };
        }
        if (method === "retry_failed") {
          const job = browserMockState.jobs.find((item) => item.id === args[0]);
          if (!job || !["failed", "cancelled", "interrupted"].includes(job.status)) return { ok: false, error: "当前任务不需要重试。" };
          if (job.job_kind === "preview") return { ok: false, error: "历史预览任务不再重试，请重新建立完整视频批次。" };
          Object.assign(job, { status: "queued", progress: 0, message: "", error_log: "", stage_label: "已重新加入队列" });
          return { ok: true, data: structuredClone(job) };
        }
        if (method === "get_production_record_groups") {
          const filters = args[0] || {};
          let records = structuredClone(browserMockLibrary.production_records || []);
          if (filters.status) records = records.filter((item) => filters.status === "active" ? item.status === "active" : item.status === filters.status);
          if (filters.novel_id) records = records.filter((item) => item.novel_id === filters.novel_id);
          if (filters.trashed === true) records = records.filter((item) => item.trashed);
          else records = records.filter((item) => !item.trashed);
          const novels = new Map();
          records.forEach((record) => {
            const novel = novels.get(record.novel_id) || { novel_id: record.novel_id, title: record.title, task_count: 0, batches: [] };
            let batch = novel.batches.find((item) => item.id === (record.batch_id || `mock-${record.novel_id}`));
            if (!batch) {
              batch = { id: record.batch_id || `mock-${record.novel_id}`, label: "演示批次", member_name: "当前账号", device_name: "当前电脑", created_at: record.created_at, status_counts: { active: 0, completed: 0, failed: 0, cancelled: 0 }, tasks: [] };
              novel.batches.push(batch);
            }
            batch.tasks.push({ ...record, raw_status: record.status, current_attempt: record.current_attempt || 1, attempts: record.attempts || [] });
            batch.status_counts[record.status] = Number(batch.status_counts[record.status] || 0) + 1;
            novel.task_count += 1;
            novels.set(record.novel_id, novel);
          });
          return { ok: true, data: { items: [...novels.values()], total_records: records.length, summary: {}, facets: { novels: [...novels.values()].map((item) => ({ id: item.novel_id, label: item.title })), batches: [], members: [], devices: [] } } };
        }
        if (method === "cancel_production_records") {
          const ids = new Set(args[0] || []);
          browserMockLibrary.production_records.forEach((item) => {
            if (ids.has(item.id)) Object.assign(item, { status: "cancelled", raw_status: "cancelled", cancellation_reason: String(args[1] || ""), cancelled_at: new Date().toISOString() });
          });
          return { ok: true, data: { cancelled: [...ids], ignored: [] } };
        }
        if (["trash_production_records", "restore_trashed_production_records", "delete_trashed_production_records"].includes(method)) {
          const ids = new Set(args[0] || []);
          if (method === "delete_trashed_production_records") browserMockLibrary.production_records = browserMockLibrary.production_records.filter((item) => !ids.has(item.id));
          else browserMockLibrary.production_records.forEach((item) => { if (ids.has(item.id)) item.trashed = method === "trash_production_records"; });
          return { ok: true, data: { records: [...ids] } };
        }
        if (method === "list_software_users") {
          return { ok: true, data: { items: structuredClone(browserMockLibrary.users || []), total: (browserMockLibrary.users || []).length } };
        }
        if (method === "save_software_user") {
          const payload = args[0] || {};
          const username = String(payload.username || "").trim();
          if (!username) return { ok: false, error: "账号名不能为空。" };
          const duplicate = (browserMockLibrary.users || []).find((item) => item.username.toLowerCase() === username.toLowerCase() && item.id !== payload.id);
          if (duplicate) return { ok: false, error: "账号名已经存在。" };
          const index = (browserMockLibrary.users || []).findIndex((item) => item.id === payload.id);
          const current = index >= 0 ? browserMockLibrary.users[index] : null;
          const user = {
            id: current?.id || `user-${Date.now()}`,
            username,
            display_name: String(payload.display_name || "").trim(),
            role: normalizeSoftwareRole(payload.role),
            active: payload.active !== false,
            has_password: Boolean(payload.initial_password || current?.has_password),
            row_version: Number(current?.row_version || 0) + 1,
            permission_overrides: { ...(current?.permission_overrides || {}) },
          };
          if (index >= 0) browserMockLibrary.users[index] = user;
          else browserMockLibrary.users.push(user);
          return { ok: true, data: structuredClone(user) };
        }
        if (method === "set_user_permission") {
          const [userId, permission, allowed] = args;
          const user = (browserMockLibrary.users || []).find((item) => item.id === userId);
          if (!user) return { ok: false, error: "没有找到软件账号。" };
          user.permission_overrides ||= {};
          if (allowed === null || allowed === undefined) delete user.permission_overrides[permission];
          else user.permission_overrides[permission] = Boolean(allowed);
          return { ok: true, data: { user_id: user.id, role: user.role, overrides: structuredClone(user.permission_overrides), effective: {} } };
        }
        if (method === "get_effective_permissions") {
          const user = (browserMockLibrary.users || []).find((item) => item.id === args[0]);
          if (!user) return { ok: false, error: "没有找到软件账号。" };
          return { ok: true, data: { user_id: user.id, role: user.role, overrides: structuredClone(user.permission_overrides || {}), effective: {} } };
        }
        if (method === "list_managed_devices") {
          const filters = args[0] || {};
          let items = browserMockState.managed_devices || [];
          if (typeof filters.active === "boolean") items = items.filter((item) => item.active === filters.active);
          if (typeof filters.online === "boolean") items = items.filter((item) => item.online === filters.online);
          const offset = Math.max(0, Number(filters.offset || 0));
          const limit = Math.max(1, Number(filters.limit || 100));
          return { ok: true, data: { items: structuredClone(items.slice(offset, offset + limit)), total: items.length, limit, offset, offline_after_seconds: 120 } };
        }
        if (method === "get_managed_device") {
          const device = (browserMockState.managed_devices || []).find((item) => item.id === args[0]);
          return device ? { ok: true, data: structuredClone(device) } : { ok: false, error: "没有找到制作电脑。" };
        }
        if (method === "rename_managed_device") {
          const device = (browserMockState.managed_devices || []).find((item) => item.id === args[0]);
          const name = String(args[1] || "").trim();
          if (!device || !name) return { ok: false, error: "没有找到制作电脑，或名称为空。" };
          device.name = name;
          device.updated_at = new Date().toISOString();
          return { ok: true, data: structuredClone(device) };
        }
        if (method === "set_managed_device_active") {
          const device = (browserMockState.managed_devices || []).find((item) => item.id === args[0]);
          if (!device) return { ok: false, error: "没有找到制作电脑。" };
          device.active = Boolean(args[1]);
          if (!device.active) {
            device.online = false;
            device.active_token_count = 0;
          }
          device.updated_at = new Date().toISOString();
          return { ok: true, data: { device: structuredClone(device), revoked_tokens: device.active ? 0 : 1 } };
        }
        if (method === "create_managed_device_config") {
          const payload = args[0] || {};
          const activeDevices = (browserMockState.managed_devices || []).filter((item) => item.active);
          const targetIds = payload.target_mode === "all"
            ? activeDevices.map((item) => item.id)
            : [...new Set(Array.isArray(payload.device_ids) ? payload.device_ids : [])];
          if (!targetIds.length) return { ok: false, error: "请至少选择一台可用电脑。" };
          const revisionNumber = Math.max(0, ...(browserMockState.managed_device_configs || []).map((item) => Number(item.revision_number || 0))) + 1;
          const now = new Date().toISOString();
          const revision = {
            id: `config-${crypto.randomUUID()}`,
            revision_number: revisionNumber,
            config_schema_version: 1,
            config: structuredClone(payload.config || {}),
            config_hash: `mock-${crypto.randomUUID().replaceAll("-", "")}`,
            target_mode: payload.target_mode,
            target_count: targetIds.length,
            target_device_ids: targetIds,
            note: String(payload.note || ""),
            created_by_user_id: "user-owner",
            created_at: now,
            targets: targetIds.map((deviceId) => {
              const device = activeDevices.find((item) => item.id === deviceId);
              if (device) device.desired_revision_number = revisionNumber;
              return { device_id: deviceId, device_name: device?.name || deviceId, device_active: true, assigned_at: now, acknowledged_at: "", ack_status: "", ack_message: "" };
            }),
          };
          browserMockState.managed_device_configs ||= [];
          browserMockState.managed_device_configs.unshift(revision);
          return { ok: true, data: structuredClone(revision) };
        }
        if (method === "list_managed_device_configs") {
          const limit = Math.max(1, Number(args[0] || 100));
          const offset = Math.max(0, Number(args[1] || 0));
          const items = browserMockState.managed_device_configs || [];
          return { ok: true, data: { items: structuredClone(items.slice(offset, offset + limit).map(({ targets: _targets, ...item }) => item)), total: items.length, limit, offset } };
        }
        if (method === "get_managed_device_config") {
          const revision = (browserMockState.managed_device_configs || []).find((item) => item.id === args[0]);
          return revision ? { ok: true, data: structuredClone(revision) } : { ok: false, error: "没有找到配置记录。" };
        }
        if (method === "get_device_sync_status") {
          return { ok: true, data: structuredClone(browserMockState.device_sync || {}) };
        }
        if (method === "get_queue_connection") {
          return { ok: true, data: { state: "connected", reconnecting: false, retry_in_seconds: 0, failures: 0, message: "队列连接正常。" } };
        }
        if (method === "sync_device_config_now") {
          const revision = (browserMockState.managed_device_configs || [])[0];
          Object.assign(browserMockState.device_sync, {
            state: "ready",
            enabled: true,
            applied_revision_id: revision?.id || "",
            last_success_at: new Date().toISOString(),
            last_error: "",
          });
          return { ok: true, data: structuredClone(browserMockState.device_sync) };
        }
        if (method === "save_platform") {
          const platform = { id: args[0].id || `platform-${Date.now()}`, ...args[0] };
          const index = browserMockState.platforms.findIndex((item) => item.id === platform.id);
          if (index >= 0) browserMockState.platforms[index] = platform;
          else browserMockState.platforms.push(platform);
          return { ok: true, data: structuredClone(platform) };
        }
        if (method === "delete_platform") {
          browserMockState.platforms = browserMockState.platforms.filter((item) => item.id !== args[0]);
          return { ok: true, data: { id: args[0] } };
        }
        if (method === "start_queue") {
          const next = browserMockState.jobs.find((job) => job.status === "queued");
          if (next) Object.assign(next, { status: "narrating", progress: 0.46, stage_label: "生成旁白" });
          return { ok: true, data: structuredClone(browserMockState.jobs) };
        }
        if (method === "cancel_queue") {
          browserMockState.jobs.forEach((job) => {
            if (!terminalStatuses.has(job.status) && job.status !== "awaiting_approval") {
              Object.assign(job, { status: "interrupted", stage_label: "已在安全节点中断" });
            }
          });
          return { ok: true, data: structuredClone(browserMockState.jobs) };
        }
        if (method === "get_jobs") {
          let active = browserMockState.jobs.find((job) => executionStatuses.has(job.status));
          if (!active) {
            active = browserMockState.jobs.find((job) => job.status === "queued");
            if (active) Object.assign(active, { status: "narrating", progress: 0.08, stage_label: "生成旁白" });
          }
          if (active) {
            active.progress = Math.min(1, Number(active.progress || 0) + 0.16);
            if (active.job_kind === "preview") {
              if (active.progress >= 1) {
                const seconds = Math.round(Number(active.settings_snapshot?.preview_seconds) || DEFAULT_PREVIEW_SECONDS);
                Object.assign(active, { status: "awaiting_approval", progress: 1, stage_label: `历史${seconds}秒预览任务待迁移`, preview_file: "browser-mock-preview.mp4", preview_uri: MOCK_PREVIEW_VIDEO_URI });
              }
            } else {
              if (active.progress >= 0.82) Object.assign(active, { status: "rendering", stage_label: "渲染成片" });
              if (active.progress >= 1) {
                Object.assign(active, { status: "completed", stage_label: "已完成", output_file: `${active.output_folder}\\${active.code}.mp4` });
              }
            }
          }
          return { ok: true, data: structuredClone(browserMockState.jobs.filter((job) => !job.archived)) };
        }
        if (method === "get_archived_jobs") {
          const options = args[0] || {};
          const limit = Math.max(1, Math.min(200, Number(options.limit || 50)));
          const offset = Math.max(0, Number(options.offset || 0));
          const archived = browserMockState.jobs.filter((job) => Boolean(job.archived));
          return {
            ok: true,
            data: {
              items: structuredClone(archived.slice(offset, offset + limit)),
              total: archived.length,
              limit,
              offset,
            },
          };
        }
        if (method === "archive_job") {
          const job = browserMockState.jobs.find((item) => item.id === args[0]);
          if (!job || !terminalStatuses.has(job.status)) return { ok: false, error: "只有已结束任务可以归档。" };
          job.archived = true;
          const archived = browserMockState.jobs.filter((item) => item.archived);
          return { ok: true, data: { current_jobs: structuredClone(browserMockState.jobs.filter((item) => !item.archived)), archived_jobs: structuredClone(archived.slice(0, 50)), archived_jobs_total: archived.length } };
        }
        if (method === "archive_batch") {
          const batchId = String(args[0] || "");
          const jobs = browserMockState.jobs.filter((item) => String(item.batch_id || "") === batchId);
          if (!jobs.length) return { ok: false, error: "没有找到这个批次。" };
          if (jobs.some((job) => !terminalStatuses.has(job.status))) {
            return { ok: false, error: "批次仍有任务在制作，结束后才能整批归档。" };
          }
          jobs.forEach((job) => { job.archived = true; });
          const archived = browserMockState.jobs.filter((item) => item.archived);
          return { ok: true, data: { current_jobs: structuredClone(browserMockState.jobs.filter((item) => !item.archived)), archived_jobs: structuredClone(archived.slice(0, 50)), archived_jobs_total: archived.length } };
        }
        if (method === "restore_job") {
          const job = browserMockState.jobs.find((item) => item.id === args[0]);
          if (!job || !job.archived) return { ok: false, error: "这个任务不在归档中。" };
          job.archived = false;
          const archived = browserMockState.jobs.filter((item) => item.archived);
          return { ok: true, data: { current_jobs: structuredClone(browserMockState.jobs.filter((item) => !item.archived)), archived_jobs: structuredClone(archived.slice(0, 50)), archived_jobs_total: archived.length } };
        }
        if (method === "restore_batch") {
          const batchId = String(args[0] || "");
          const jobs = browserMockState.jobs.filter((item) => String(item.batch_id || "") === batchId);
          if (!jobs.length) return { ok: false, error: "没有找到这个批次。" };
          jobs.forEach((job) => { job.archived = false; });
          const archived = browserMockState.jobs.filter((item) => item.archived);
          return { ok: true, data: { current_jobs: structuredClone(browserMockState.jobs.filter((item) => !item.archived)), archived_jobs: structuredClone(archived.slice(0, 50)), archived_jobs_total: archived.length } };
        }
        if (method === "archive_finished_jobs") {
          browserMockState.jobs.forEach((job) => {
            if (terminalStatuses.has(job.status)) job.archived = true;
          });
          const archived = browserMockState.jobs.filter((item) => item.archived);
          return { ok: true, data: { current_jobs: structuredClone(browserMockState.jobs.filter((item) => !item.archived)), archived_jobs: structuredClone(archived.slice(0, 50)), archived_jobs_total: archived.length } };
        }
        if (method === "open_output_folder") return { ok: true, data: { path: args[0] } };
        if (method === "get_hub_status") {
          const hub = browserMockState.settings.hub || {};
          const runtime = browserMockState.hub_status || {};
          const runtimeMode = runtime.runtime_mode || runtime.mode || "local";
          const publicMode = runtimeMode === "embedded" ? "local" : runtimeMode;
          const running = publicMode === "host" && runtime.running !== false;
          const connected = publicMode !== "client" || runtime.connected !== false;
          const ready = publicMode === "local" || running || (publicMode === "client" && connected);
          const restartRequired = (hub.mode || "local") !== publicMode;
          return { ok: true, data: { ...structuredClone(runtime), configured_mode: hub.mode || "local", runtime_mode: runtimeMode, mode: publicMode, device_name: hub.device_name || "This PC", endpoint: hub.endpoint || "", listen_port: Number(hub.listen_port || 8765), running, connected, online: ready, status: ready ? "ready" : "offline", restart_required: restartRequired, message: restartRequired ? "设置已保存，请重启 StoryForge 让模式切换生效。" : publicMode === "host" ? "本机 Hub 已准备长期运行。" : publicMode === "client" ? "已连接 StoryForge Hub。" : "本机独立运行，不同步其他电脑。" } };
        }
        if (method === "connect_hub_with_password") {
          const [endpoint, username, password, deviceName] = args;
          if (!endpoint || !username || !password || !deviceName) return { ok: false, error: "请输入员工账号和密码；连接设置会自动使用预设值。" };
          Object.assign(browserMockState.settings.hub, {
            mode: "client",
            endpoint: String(endpoint),
            account_username: String(username),
            device_name: String(deviceName),
            has_access_token: true,
          });
          Object.assign(browserMockState.hub_status, { runtime_mode: "client", mode: "client", connected: true, online: true, status: "ready" });
          return { ok: true, data: { ...structuredClone(browserMockState.hub_status), settings: structuredClone(browserMockState.settings), member: { username: String(username), role: "producer" } } };
        }
        if (method === "get_update_status") return { ok: true, data: structuredClone(browserMockState.update_status) };
        if (method === "check_for_updates") {
          Object.assign(browserMockState.update_status, { available_version: "2.4.0", state: "available", message: "发现 StoryForge 2.4.0，可以从主电脑安全下载。", checked_at: new Date().toISOString(), downloaded: false, apply_on_restart: false });
          return { ok: true, data: structuredClone(browserMockState.update_status) };
        }
        if (method === "download_update") {
          Object.assign(browserMockState.update_status, { state: "downloaded", downloaded: true, message: "更新包已下载并验证，可以安排重启安装。" });
          return { ok: true, data: structuredClone(browserMockState.update_status) };
        }
        if (method === "schedule_update_on_restart") {
          Object.assign(browserMockState.update_status, { state: "scheduled", apply_on_restart: true, restart_required: true, message: "已安排在下次重启 StoryForge 时安装。" });
          return { ok: true, data: structuredClone(browserMockState.update_status) };
        }
        if (method === "restart_to_apply_update") {
          Object.assign(browserMockState.update_status, { state: "applying_on_restart", apply_on_restart: true, restart_required: true, exit_queued: true, message: "StoryForge 正在安全退出，更新完成后会自动重新打开。" });
          return { ok: true, data: structuredClone(browserMockState.update_status) };
        }
        if (method === "cancel_scheduled_update") {
          Object.assign(browserMockState.update_status, { state: "downloaded", apply_on_restart: false, restart_required: false, message: "已取消重启安装，下载的更新包仍保留。" });
          return { ok: true, data: structuredClone(browserMockState.update_status) };
        }
        if (method === "save_local_update_preferences") {
          const patch = args[0] || {};
          Object.assign(browserMockState.settings.hub, patch);
          return { ok: true, data: { settings: structuredClone(browserMockState.settings), update_status: structuredClone(browserMockState.update_status) } };
        }
        if (method === "publish_update") {
          const [packagePath, version, releaseNotes] = args;
          if (!packagePath || !version) return { ok: false, error: "请选择更新包并填写版本号。" };
          Object.assign(browserMockState.update_status, { available_version: String(version), state: "published", message: `已向团队发布 StoryForge ${version}。`, package_path: String(packagePath), release_notes: String(releaseNotes || ""), checked_at: new Date().toISOString() });
          return { ok: true, data: structuredClone(browserMockState.update_status) };
        }
        if (method === "clear_published_update") {
          Object.assign(browserMockState.update_status, { available_version: "", state: "up_to_date", message: "主电脑当前没有对外发布更新。", package_path: "" });
          return { ok: true, data: structuredClone(browserMockState.update_status) };
        }
        if (method === "save_settings") {
          const patch = args[0] || {};
          browserMockState.settings = { ...browserMockState.settings, ...patch };
          ["subtitle", "intro_card", "code_card", "outro_card", "providers", "hub"].forEach((key) => {
            if (patch[key]) browserMockState.settings[key] = { ...(mockBootstrap.settings[key] || {}), ...(browserMockState.settings[key] || {}), ...patch[key] };
          });
          if (args[0]?.hub) browserMockState.settings.hub.has_access_token = Boolean(args[0].hub.access_token && args[0].hub.access_token !== "********");
          return { ok: true, data: structuredClone(browserMockState.settings) };
        }
      }
      return {
        ok: false,
        error: isWebRuntime
          ? "网页服务尚未就绪，请确认 StoryForge Hub 正在运行。"
          : "桌面服务尚未就绪，请稍候重试。",
      };
    },
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function toast(message, kind = "info") {
    const item = document.createElement("div");
    item.className = `toast ${kind === "error" ? "error" : ""}`;
    item.setAttribute("role", kind === "error" ? "alert" : "status");
    item.textContent = message;
    $("#toast-region").append(item);
    window.setTimeout(() => item.remove(), 4200);
  }

  async function checkedCall(method, ...args) {
    try {
      const result = await bridge.call(method, ...args);
      if (!result || !result.ok) {
        if (responseRequiresClientUpdate(null, result)) showClientUpdateRequired(result);
        throw new Error(apiFailureMessage(result));
      }
      return result.data;
    } catch (error) {
      if (responseRequiresClientUpdate(null, error)) showClientUpdateRequired(error);
      throw error;
    }
  }

  function navigate(viewName) {
    if (viewName !== "hub") stopManagedDeviceFleetPolling();
    if (viewName !== "library" && state.librarySelectionMode) {
      state.librarySelectionMode = "";
    }
    const navView = ["platforms", "publishing"].includes(viewName)
      ? "library"
      : ["styles", "providers", "hub", "accounts"].includes(viewName)
        ? "settings"
        : viewName;
    $$("[data-view-target]").forEach((item) => {
      const active = item.dataset.viewTarget === navView;
      item.classList.toggle("is-active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    $$("[data-view]").forEach((item) => {
      item.classList.toggle("is-visible", item.dataset.view === viewName);
    });
    if (viewName !== "library" && !$("#novel-detail-drawer")?.classList.contains("is-hidden")) closeNovelDetail();
    if (viewName === "library") renderNovelLibrary();
    if (viewName === "queue") renderProductionWorkbench();
    if (viewName === "settings") renderSettingsAccess();
    if (viewName === "platforms") renderPlatforms();
    if (viewName === "styles") updateStylePreview();
    if (viewName === "providers") applyProviderAccessMode();
    if (viewName === "publishing") renderPublishingAccounts();
    if (viewName === "records") {
      renderRecords();
      void loadProductionRecordGroups({ silent: true });
    }
    if (viewName === "accounts") {
      renderSoftwareUsers();
    }
    if (viewName === "hub") {
      renderManagedDeviceWorkspace();
      renderDeviceSyncStatus();
      void refreshHubDeviceWorkspace({ silent: true });
      startManagedDeviceFleetPolling();
    }
    if (window.matchMedia("(max-width: 980px)").matches) window.scrollTo({ top: 0, behavior: "smooth" });
    else $(".workspace")?.scrollTo({ top: 0, behavior: "smooth" });
  }

  function formPayload(form) {
    return Object.fromEntries(new FormData(form).entries());
  }

  const terminalStatuses = new Set(["completed", "failed", "cancelled", "interrupted"]);
  const executionStatuses = new Set(["preflight", "preparing", "polishing", "narrating", "composing", "previewing", "rendering"]);

  function pathLeaf(value) {
    const parts = String(value || "").split(/[\\/]+/).filter(Boolean);
    return parts.at(-1) || "未命名文件夹";
  }

  function pathParent(value) {
    const clean = String(value || "").trim().replace(/[\\/]+$/, "");
    const separator = Math.max(clean.lastIndexOf("\\"), clean.lastIndexOf("/"));
    return separator > 0 ? clean.slice(0, separator) : "";
  }

  function jobResolvedOutputFolder(job) {
    const publishBatchFolder = String(
      job?.publish_batch_folder || job?.metadata?.publish_batch_folder || "",
    ).trim();
    if (publishBatchFolder) return publishBatchFolder;
    const renderedFileFolder = pathParent(job?.output_file || job?.output_path || "");
    if (renderedFileFolder) return renderedFileFolder;
    return String(job?.output_folder || "").trim();
  }

  function batchResolvedOutputFolder(jobs) {
    const values = Array.isArray(jobs) ? jobs : [];
    const published = values
      .map((job) => String(job?.publish_batch_folder || job?.metadata?.publish_batch_folder || "").trim())
      .find(Boolean);
    if (published) return published;
    const rendered = values
      .map((job) => pathParent(job?.output_file || job?.output_path || ""))
      .find(Boolean);
    if (rendered) return rendered;
    return values.map((job) => String(job?.output_folder || "").trim()).find(Boolean) || "";
  }

  function platformBrandColor(platform) {
    const value = String(platform?.brand_color || "").trim();
    const short = value.match(/^#([0-9a-f]{3})$/i);
    if (short) return `#${[...short[1]].map((item) => item + item).join("")}`;
    return /^#[0-9a-f]{6}$/i.test(value) ? value : "#315bd8";
  }

  function platformLogoSource(platform) {
    // Platform branding is intentionally isolated from novel.cover_uri and
    // novel.cover_path: a book cover must never be presented as an app logo.
    return String(platform?.logo_uri || platform?.logo_path || "").trim();
  }

  function platformInitial(platform) {
    return String(platform?.name || "P").trim().slice(0, 1).toUpperCase() || "P";
  }

  function platformLogoInnerMarkup(platform, source = platformLogoSource(platform)) {
    const initial = escapeHtml(platformInitial(platform));
    const usableSource = webAssetUrl(source);
    const image = usableSource
      ? `<img src="${escapeHtml(usableSource)}" alt="" data-platform-logo-image />`
      : "";
    return `<span class="platform-logo-fallback">${initial}</span>${image}`;
  }

  function platformLogoMarkup(platform, className = "platform-letter") {
    const label = `${String(platform?.name || "Platform")} Logo`;
    return `<span class="${escapeHtml(className)} platform-brand-mark" style="--platform-brand: ${platformBrandColor(platform)}" aria-label="${escapeHtml(label)}">${platformLogoInnerMarkup(platform)}</span>`;
  }

  function activatePlatformLogoFallbacks(root = document) {
    $$('[data-platform-logo-image]', root).forEach((image) => {
      image.addEventListener("error", () => image.remove(), { once: true });
    });
  }

  function paintPlatformLogo(element, platform, source = platformLogoSource(platform)) {
    if (!element) return;
    element.style.setProperty("--platform-brand", platformBrandColor(platform));
    element.innerHTML = platformLogoInnerMarkup(platform, source);
    element.dataset.hasLogo = source ? "true" : "false";
    activatePlatformLogoFallbacks(element);
  }

  async function withBusyButton(button, busyLabel, operation) {
    if (!button || button.getAttribute("aria-busy") === "true") return undefined;
    const content = button.innerHTML;
    const wasDisabled = button.disabled;
    const labelNode = $("#start-queue-label", button);
    const labelText = labelNode?.textContent || "";
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    if (labelNode) labelNode.textContent = busyLabel;
    else button.textContent = busyLabel;
    try {
      return await operation();
    } finally {
      if (labelNode?.isConnected) labelNode.textContent = labelText;
      else button.innerHTML = content;
      button.removeAttribute("aria-busy");
      button.disabled = wasDisabled;
    }
  }

  function renderPlatformOptions() {
    renderLibraryPlatformFilter();
    renderNovelLibrary();
    renderPublishingAccountOptions();
    renderProductionWorkbench();
  }

  function renderPlatforms() {
    const root = $("#platform-list");
    if (!state.platforms.length) {
      root.innerHTML = '<div class="platform-empty">还没有平台档案。点击“新平台”建立第一套口令。</div>';
      return;
    }
    root.innerHTML = state.platforms
      .map(
        (platform) => `
          <button class="platform-item ${platform.id === state.selectedPlatformId ? "is-active" : ""}" data-platform-id="${escapeHtml(platform.id)}">
            ${platformLogoMarkup(platform)}
            <span><b>${escapeHtml(platform.name)}</b><small>${escapeHtml(platform.search_template)}</small></span>
            <em>编辑 →</em>
          </button>`,
      )
      .join("");
    activatePlatformLogoFallbacks(root);
    $$("[data-platform-id]", root).forEach((item) => {
      item.addEventListener("click", () => editPlatform(item.dataset.platformId));
    });
  }

  function renderPublishingAccountOptions() {
    const form = $("#publishing-account-form");
    const filter = $("#publishing-platform-filter");
    const selectedForm = form?.elements.platform_id?.value || "";
    const selectedFilter = state.publishingPlatformFilter;
    const options = state.platforms
      .map((platform) => `<option value="${escapeHtml(platform.id)}">${escapeHtml(platform.name)}</option>`)
      .join("");
    if (form?.elements.platform_id) {
      form.elements.platform_id.innerHTML = `<option value="">选择平台</option>${options}`;
      form.elements.platform_id.value = state.platforms.some((item) => item.id === selectedForm) ? selectedForm : "";
    }
    if (filter) {
      filter.innerHTML = `<option value="">全部平台</option>${options}`;
      filter.value = state.platforms.some((item) => item.id === selectedFilter) ? selectedFilter : "";
    }
  }

  function resetPublishingAccountEditor() {
    const form = $("#publishing-account-form");
    if (!form) return;
    form.reset();
    form.elements.id.value = "";
    form.elements.active.checked = true;
    state.selectedPublishingAccountId = "";
    $("#publishing-editor-title").textContent = "新建发布账号";
    renderPublishingAccountOptions();
    renderPublishingAccounts();
  }

  function editPublishingAccount(accountId) {
    const account = state.publishingAccounts.find((item) => item.id === accountId);
    const form = $("#publishing-account-form");
    if (!account || !form) return;
    state.selectedPublishingAccountId = account.id;
    for (const key of ["id", "platform_id", "name", "handle", "region", "positioning", "notes"]) {
      if (form.elements[key]) form.elements[key].value = account[key] || "";
    }
    form.elements.active.checked = account.active !== false;
    $("#publishing-editor-title").textContent = `编辑 ${account.name}`;
    renderPublishingAccounts();
  }

  function renderPublishingAccounts() {
    const root = $("#publishing-account-list");
    if (!root) return;
    renderPublishingAccountOptions();
    const filter = $("#publishing-platform-filter");
    if (filter) filter.disabled = !state.publishingAccounts.length;
    const accounts = state.publishingAccounts.filter((item) => (
      !state.publishingPlatformFilter || item.platform_id === state.publishingPlatformFilter
    ));
    $("#publishing-account-count").textContent = `${accounts.length} 个账号`;
    if (!accounts.length) {
      const filtered = Boolean(state.publishingPlatformFilter && state.publishingAccounts.length);
      root.innerHTML = `<div class="publishing-empty">
        <span class="publishing-empty-mark" aria-hidden="true">A</span>
        <h3>${filtered ? "该平台暂无账号" : "还没有发布账号"}</h3>
        <p>${filtered ? "可以查看全部平台，或直接为当前平台新建账号。" : "创建后，员工就能在制作台按平台直接选择。"}</p>
        <div>${filtered ? '<button type="button" class="button button-ghost" data-clear-publishing-filter>查看全部平台</button>' : ""}<button type="button" class="button button-secondary" data-create-publishing-account>新建账号</button></div>
      </div>`;
      return;
    }
    root.innerHTML = accounts.map((account) => {
      const platform = platformById(account.platform_id);
      return `<button type="button" class="publishing-account-item ${account.id === state.selectedPublishingAccountId ? "is-active" : ""} ${account.active === false ? "is-disabled" : ""}" data-publishing-account-id="${escapeHtml(account.id)}">
        <span class="publishing-account-mark">${escapeHtml((account.name || "A").slice(0, 1).toUpperCase())}</span>
        <span><b>${escapeHtml(account.name || "未命名账号")}</b><small>${escapeHtml(platform?.name || "未绑定平台")} · ${escapeHtml(account.handle || "账号标识待补充")}${account.region ? ` · ${escapeHtml(account.region)}` : ""}</small></span>
        <em class="${account.active === false ? "is-off" : "is-on"}">${account.active === false ? "已停用" : "可使用"}</em>
      </button>`;
    }).join("");
  }

  function resetPlatformEditor() {
    const form = $("#platform-form");
    form.reset();
    form.elements.id.value = "";
    form.elements.search_template.value = "Search {platform}: {code}";
    form.elements.ending_template.value =
      "Download {platform} and search code {code} to continue reading.";
    form.elements.logo_path.value = "";
    form.elements.brand_color.value = "#315bd8";
    state.selectedPlatformId = "";
    state.platformLogoPreviewSource = "";
    $("#platform-editor-title").textContent = "新建平台";
    $("#delete-platform").classList.add("is-hidden");
    renderPlatforms();
    updatePlatformTemplatePreview();
    updatePlatformBrandPreview();
  }

  function editPlatform(platformId) {
    const platform = state.platforms.find((item) => item.id === platformId);
    if (!platform) return;
    state.selectedPlatformId = platformId;
    const form = $("#platform-form");
    Object.entries(platform).forEach(([key, value]) => {
      if (form.elements[key]) form.elements[key].value = value;
    });
    form.elements.logo_path.value = platform.shared_logo_path || platform.logo_path || "";
    form.elements.brand_color.value = platformBrandColor(platform);
    state.platformLogoPreviewSource = platformLogoSource(platform);
    $("#platform-editor-title").textContent = `编辑 ${platform.name}`;
    $("#delete-platform").classList.remove("is-hidden");
    renderPlatforms();
    updatePlatformTemplatePreview();
    updatePlatformBrandPreview();
  }

  function safeTemplate(template, platform, code = "123456") {
    return String(template || "")
      .replaceAll("{platform}", platform || "Platform")
      .replaceAll("{code}", code);
  }

  function updatePlatformTemplatePreview() {
    const form = $("#platform-form");
    const text = safeTemplate(
      form.elements.search_template.value,
      form.elements.name.value || "Platform",
    );
    $("#platform-template-preview").textContent = text;
  }

  function updatePlatformBrandPreview() {
    const form = $("#platform-form");
    if (!form) return;
    const selected = state.platforms.find((item) => item.id === state.selectedPlatformId) || {};
    const platform = {
      ...selected,
      name: form.elements.name.value || selected.name || "Platform",
      brand_color: form.elements.brand_color.value || selected.brand_color || "#315bd8",
    };
    const source = String(state.platformLogoPreviewSource || "").trim();
    const storedPath = String(form.elements.logo_path.value || "").trim();
    paintPlatformLogo($("#platform-logo-preview"), platform, source);
    $("#platform-brand-editor").style.setProperty("--platform-brand", platformBrandColor(platform));
    $("#platform-logo-title").textContent = source
      ? "平台 Logo 已选择"
      : storedPath
        ? "共享 Logo 暂不可预览"
        : "使用平台首字母";
    $("#platform-logo-file").textContent = source || storedPath
      ? `${webAssetDisplayName(source || storedPath)} · 保存后同步到其他电脑`
      : "尚未选择 Logo；不会使用小说封面代替。";
    $("#clear-platform-logo").disabled = !source && !storedPath;
  }

  async function choosePlatformLogo(button) {
    try {
      await withBusyButton(button, "正在选择…", async () => {
        const logoPath = await checkedCall("choose_file", "cover");
        if (!logoPath) return;
        const form = $("#platform-form");
        form.elements.logo_path.value = String(logoPath);
        state.platformLogoPreviewSource = String(logoPath);
        updatePlatformBrandPreview();
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function clearPlatformLogo() {
    const form = $("#platform-form");
    form.elements.logo_path.value = "";
    state.platformLogoPreviewSource = "";
    updatePlatformBrandPreview();
  }

  function platformById(platformId) {
    return state.platforms.find((item) => item.id === platformId) || null;
  }

  function bindingFor(novel, platformId = "") {
    const target = platformId || novel?.draft?.platform_id || novel?.platform_bindings?.[0]?.platform_id || "";
    return novel?.platform_bindings?.find((item) => item.platform_id === target) || null;
  }

  function coverToneClass(value) {
    const allowed = new Set(["ember", "violet", "noir", "cobalt"]);
    return allowed.has(value) ? `tone-${value}` : "tone-cobalt";
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return `${minutes}:${String(rest).padStart(2, "0")}`;
  }

  function episodeStatusLabel(status) {
    return { completed: "已完成", failed: "失败", ready: "待制作", active: "制作中" }[status] || "待制作";
  }

  function upsertNovel(novel) {
    if (!novel?.id) return;
    const index = state.novels.findIndex((item) => item.id === novel.id);
    if (index >= 0) state.novels[index] = novel;
    else state.novels.unshift(novel);
    if (state.selectedNovelId === novel.id) state.selectedNovel = novel;
    if (state.productionNovelId === novel.id) state.productionNovel = novel;
  }

  function episodeSpineMarkup(novel, compact = false) {
    const episodes = Array.isArray(novel.episodes) ? novel.episodes : [];
    const visible = compact ? episodes.slice(0, 5) : episodes;
    const nodes = visible
      .map(
        (episode) => `
          <span class="spine-node is-${escapeHtml(episode.status || "ready")}" title="第${episode.number}集 · ${escapeHtml(episode.title)} · ${episodeStatusLabel(episode.status)}">
            <i>${episode.number}</i>${compact ? "" : `<b>${escapeHtml(episode.title)}</b><small>${escapeHtml(episode.source_label)} · ${formatDuration(episode.duration_seconds)}</small>`}
          </span>`,
      )
      .join("");
    const more = compact && episodes.length > visible.length ? `<span class="spine-more">+${episodes.length - visible.length}</span>` : "";
    return `<div class="series-spine ${compact ? "is-compact" : ""}" aria-label="${episodes.length}个视频分集">${nodes}${more}</div>`;
  }

  const languageCatalog = [
    ["en", "英语", "EN"],
    ["zh", "中文", "中"],
    ["es", "西班牙语", "ES"],
    ["pt", "葡萄牙语", "PT"],
    ["id", "印度尼西亚语", "ID"],
    ["ja", "日语", "日"],
    ["ko", "韩语", "한"],
    ["fr", "法语", "FR"],
    ["de", "德语", "DE"],
    ["it", "意大利语", "IT"],
    ["hi", "印地语", "HI"],
  ];
  const languageCatalogByCode = new Map(languageCatalog.map((item) => [item[0], item]));
  const languageEditorCatalog = [
    ["en", "英语"],
    ["zh-Hans", "简体中文"],
    ["zh-Hant", "繁体中文"],
    ["es", "西班牙语"],
    ["pt", "葡萄牙语"],
    ["id", "印度尼西亚语"],
    ["fr", "法语"],
    ["de", "德语"],
    ["it", "意大利语"],
    ["hi", "印地语"],
    ["ja", "日语"],
    ["ko", "韩语"],
  ];

  function canonicalLanguageKey(code) {
    const normalized = String(code || "").trim().toLocaleLowerCase().replaceAll("_", "-");
    if (!normalized || ["und", "unknown", "auto"].includes(normalized)) return "und";
    const base = normalized.split("-")[0];
    const aliases = { cmn: "zh", zho: "zh", eng: "en", spa: "es", ita: "it", hin: "hi" };
    return aliases[base] || base;
  }

  function languageEditorCode(code) {
    const normalized = String(code || "").trim().replaceAll("_", "-");
    const folded = normalized.toLocaleLowerCase();
    if (["zh-hant", "zh-tw", "zh-hk"].includes(folded)) return "zh-Hant";
    if (["zh", "zh-hans", "zh-cn", "cmn", "zho"].includes(folded)) return "zh-Hans";
    const base = canonicalLanguageKey(normalized);
    return languageEditorCatalog.some(([itemCode]) => itemCode === base) ? base : "";
  }

  function novelLanguageInfo(novel) {
    const legacyLanguage = novel?.language;
    const detection = novel?.language_detection
      || (legacyLanguage && typeof legacyLanguage === "object" ? legacyLanguage : {});
    const code = detection?.code || (typeof legacyLanguage === "string" ? legacyLanguage : "und");
    const key = canonicalLanguageKey(code);
    const catalogItem = languageCatalogByCode.get(key);
    const rawConfidence = Number(detection?.confidence);
    const confidence = Number.isFinite(rawConfidence)
      ? Math.max(0, Math.min(1, rawConfidence > 1 ? rawConfidence / 100 : rawConfidence))
      : null;
    const source = String(detection?.source || (detection?.manual ? "manual" : "legacy")).toLocaleLowerCase();
    const manual = ["manual", "override", "user", "user_override"].includes(source);
    const label = String(detection?.display_name || detection?.label || catalogItem?.[1] || (key === "und" ? "待识别" : key.toLocaleUpperCase()));
    return {
      code: String(code || "und"),
      key,
      label,
      shortLabel: catalogItem?.[2] || (key === "und" ? "?" : key.slice(0, 3).toLocaleUpperCase()),
      confidence,
      source,
      manual,
      lowConfidence: key === "und" || (confidence !== null && confidence < 0.75),
    };
  }

  function mockLanguageDetection(payload, content) {
    const forcedCode = String(payload?.language || "").trim();
    const text = String(content || "");
    let code = forcedCode ? languageEditorCode(forcedCode) : "";
    let confidence = forcedCode ? 1 : 0.96;
    if (!forcedCode) {
      const italianHits = text.match(/\b(perché|però|nessuno|qualcosa|niente|allora|disse|chiese|rispose|sapeva|guardò|sentì|tornò)\b/giu)?.length || 0;
      if (/[\u0900-\u097f]/u.test(text)) code = "hi";
      else if (/[\u3400-\u9fff]/u.test(text)) code = "zh";
      else if (italianHits >= 3) code = "it";
      else if (/[¿¡ñáéíóúü]/iu.test(text) || /\b(una|pero|para|que|con|del|ella|él)\b/iu.test(text)) code = "es";
      else code = "en";
    }
    const catalogItem = languageCatalogByCode.get(canonicalLanguageKey(code));
    const displayName = code === "zh-Hans" ? "简体中文" : code === "zh-Hant" ? "繁体中文" : catalogItem?.[1] || code.toLocaleUpperCase();
    return { code, display_name: displayName, confidence, source: forcedCode ? "manual" : "auto" };
  }

  function languageConfidenceText(info) {
    if (info.manual) return "人工确认";
    if (info.confidence === null) return "自动识别";
    return `自动识别 ${Math.round(info.confidence * 100)}%`;
  }

  function languageBadgeMarkup(novel, { detailed = false } = {}) {
    const info = novelLanguageInfo(novel);
    const classes = ["language-badge", info.lowConfidence ? "is-low-confidence" : "", info.manual ? "is-manual" : ""]
      .filter(Boolean)
      .join(" ");
    const status = info.lowConfidence ? '<em>待确认</em>' : detailed ? `<small>${escapeHtml(languageConfidenceText(info))}</small>` : "";
    const title = `${info.label} · ${languageConfidenceText(info)}${info.lowConfidence ? "，建议打开详情确认" : ""}`;
    return `<span class="${classes}" title="${escapeHtml(title)}" aria-label="${escapeHtml(title)}"><b>${escapeHtml(info.shortLabel)}</b>${detailed ? `<span>${escapeHtml(info.label)}</span>` : ""}${status}</span>`;
  }

  function languageEditorOptions(selectedCode = "") {
    const selectedKey = languageEditorCode(selectedCode);
    const options = languageEditorCatalog.map(([code, label]) => `<option value="${code}" ${selectedKey === code ? "selected" : ""}>${label}</option>`).join("");
    return `<option value="">使用自动识别</option>${options}`;
  }

  function syncImportLanguageOptions() {
    const select = $("#import-language");
    if (!select) return;
    const selected = languageEditorCode(select.value);
    select.innerHTML = `<option value="">自动识别（推荐）</option>${languageEditorCatalog
      .map(([code, label]) => `<option value="${code}">${escapeHtml(label)}</option>`)
      .join("")}`;
    select.value = selected;
  }

  function renderLibraryLanguageFilter() {
    const root = $("#library-language-filter");
    if (!root) return;
    const languageCounts = new Map();
    state.novels.forEach((novel) => {
      const info = novelLanguageInfo(novel);
      const entry = languageCounts.get(info.key) || { ...info, count: 0 };
      entry.count += 1;
      languageCounts.set(info.key, entry);
    });
    if (state.libraryLanguageFilter && !languageCounts.has(state.libraryLanguageFilter)) {
      state.libraryLanguageFilter = "";
    }
    const order = new Map(languageCatalog.map((item, index) => [item[0], index]));
    const entries = [...languageCounts.values()].sort((left, right) => {
      const knownOrder = (order.get(left.key) ?? 999) - (order.get(right.key) ?? 999);
      return knownOrder || left.label.localeCompare(right.label, "zh-CN");
    });
    root.innerHTML = `<button type="button" class="${state.libraryLanguageFilter ? "" : "is-active"}" data-language-filter="" aria-pressed="${String(!state.libraryLanguageFilter)}">全部 <span>${state.novels.length}</span></button>${entries.map((info) => {
      const active = state.libraryLanguageFilter === info.key;
      return `<button type="button" class="${active ? "is-active" : ""}" data-language-filter="${escapeHtml(info.key)}" aria-pressed="${String(active)}"><b>${escapeHtml(info.shortLabel)}</b>${escapeHtml(info.label)} <span>${info.count}</span></button>`;
    }).join("")}`;
  }

  function workbenchLanguageNoticeMarkup(novel) {
    const info = novelLanguageInfo(novel);
    const notices = [];
    const selectedProvider = String(effectiveTtsProviders().tts_provider || "local_kokoro").toLocaleLowerCase().replaceAll("-", "_");
    const localKokoroLanguages = new Set(["en", "ja", "es", "fr", "hi", "it", "pt", "zh"]);
    const edgeLanguages = new Set(["en", "ja", "es", "fr", "de", "id", "ko", "it", "pt", "hi", "zh"]);
    if (info.lowConfidence) {
      notices.push(`<div class="workbench-language-alert is-review"><b>先确认这部小说的语种</b><span>当前识别为${escapeHtml(info.label)}（${escapeHtml(languageConfidenceText(info))}）。请在小说详情确认后再检查配音与字幕。</span><button type="button" class="text-button" data-open-production-novel-profile="${escapeHtml(novel.id)}">去确认</button></div>`);
    }
    if (["edge", "edge_tts", "microsoft_edge", "microsoft_edge_tts"].includes(selectedProvider) && info.key !== "und" && edgeLanguages.has(info.key)) {
      const runtimeReady = Boolean(effectiveTtsSystem().edge_tts_runtime_ready);
      notices.push(`<div class="workbench-language-alert is-provider ${runtimeReady ? "is-ready" : ""}"><b>${runtimeReady ? `可试听${escapeHtml(info.label)}在线女声` : "当前电脑缺少 Edge TTS 组件"}</b><span>${runtimeReady ? "生成候选时会联网读取该语种当前真实可用的女声，最多展示3个；无法连接时不会用假声线代替。" : "安装 requirements.txt 中的 Edge TTS 后重启；无需 API Key，但试听和制作时需要联网。"}</span><button type="button" class="text-button" data-open-view="providers">查看服务</button></div>`);
    } else if (["local", "kokoro", "local_kokoro", "kokoro_local", "kokoro_http", "kokoro_cli"].includes(selectedProvider) && info.key !== "en" && info.key !== "und" && localKokoroLanguages.has(info.key)) {
      const providers = effectiveTtsProviders();
      const runtimeReady = Boolean(
        effectiveTtsSystem().embedded_kokoro_ready
        || providers.kokoro_endpoint
        || localTtsRuntimeSnapshot()?.kokoro_configured,
      );
      notices.push(`<div class="workbench-language-alert is-provider ${runtimeReady ? "is-ready" : ""}"><b>${runtimeReady ? `已支持${escapeHtml(info.label)}免费本地配音` : "当前电脑缺少 Kokoro 本地语音组件"}</b><span>${runtimeReady ? "会按小说语种自动匹配当前电脑真实可用的 Kokoro 女声。" : `暂时不能用 Kokoro 生成${escapeHtml(info.label)}配音；可先切换 Edge TTS，或安装完整本地语音包后重启。`}</span><button type="button" class="text-button" data-open-view="providers">${runtimeReady ? "查看语音包" : "切换或安装"}</button></div>`);
    } else if (["deepgram", "deepgram_aura", "aura", "aura_2"].includes(selectedProvider) && info.key === "ja") {
      notices.push(`<div class="workbench-language-alert is-provider is-ready"><b>Deepgram 已提供日语女声</b><span>会读取当前配置的真实日语候选；需要有效 API Key。</span><button type="button" class="text-button" data-open-view="providers">查看服务</button></div>`);
    } else if (info.key !== "en" && info.key !== "und") {
      notices.push(`<div class="workbench-language-alert is-provider"><b>${escapeHtml(info.label)}需要云端多语种配音</b><span>当前免费本地包未覆盖该语种；可配置有免费额度的云端服务后生成。</span><button type="button" class="text-button" data-open-view="providers">配置服务</button></div>`);
    }
    return notices.length ? `<div class="workbench-language-alerts">${notices.join("")}</div>` : "";
  }

  function librarySearchText(novel) {
    const codes = (novel.platform_bindings || []).flatMap((binding) => (binding.codes || []).map((code) => code.value));
    const language = novelLanguageInfo(novel);
    const classification = novel.story_classification || {};
    return [novel.title, novel.synopsis, ...(novel.tags || []), ...codes, language.code, language.label, classification.mood, classification.label].join(" ").toLocaleLowerCase();
  }

  function filteredNovels() {
    const query = state.libraryQuery.trim().toLocaleLowerCase();
    return state.novels.filter((novel) => {
      const queryMatches = !query || librarySearchText(novel).includes(query);
      const platformMatches = !state.libraryPlatformFilter
        || (novel.platform_bindings || []).some((binding) => binding.platform_id === state.libraryPlatformFilter);
      const languageMatches = !state.libraryLanguageFilter
        || novelLanguageInfo(novel).key === state.libraryLanguageFilter;
      return queryMatches && platformMatches && languageMatches;
    });
  }

  function renderLibraryPlatformFilter() {
    const select = $("#library-platform-filter");
    if (!select) return;
    const selected = state.libraryPlatformFilter;
    select.innerHTML = '<option value="">全部平台</option>' + state.platforms
      .map((platform) => `<option value="${escapeHtml(platform.id)}">${escapeHtml(platform.name)}</option>`)
      .join("");
    select.value = state.platforms.some((item) => item.id === selected) ? selected : "";
  }

  function renderLibraryFailureBanner() {
    const root = $("#library-failure-banner");
    if (!root) return;
    const failedLibrary = state.productionRecords.filter((item) => item.status === "failed");
    const failedQueue = state.jobs.filter((item) => !item.archived && item.status === "failed");
    const count = failedLibrary.length + failedQueue.length;
    root.classList.toggle("is-hidden", count === 0);
    if (!count) return;
    $("#library-failure-title").textContent = `${count} 个失败任务已跳过`;
    const latest = failedLibrary[0]?.error || failedQueue[0]?.message || "失败原因已记录。";
    $("#library-failure-copy").textContent = `${latest} 其余视频仍会继续制作。`;
  }

  function renderNovelLibrary() {
    const root = $("#novel-list");
    if (!root) return;
    const selectingForProduction = state.librarySelectionMode === "production";
    $("#library-production-picker")?.classList.toggle("is-hidden", !selectingForProduction);
    const search = $("#library-search");
    if (search) search.placeholder = selectingForProduction
      ? "搜索要制作的小说：书名、标签、口令或语种"
      : "搜索书名、标签或口令";
    renderLibraryPlatformFilter();
    renderLibraryLanguageFilter();
    renderLibraryFailureBanner();
    $$('[data-library-layout]').forEach((button) => {
      const active = button.dataset.libraryLayout === state.libraryLayout;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    if (!state.libraryBackendReady && !state.novels.length) {
      const detail = String(state.libraryBackendError || "小说库暂时无法读取，请检查主电脑连接后重试。");
      root.className = "library-content";
      root.innerHTML = `
        <div class="empty-state library-empty backend-empty">
          <div class="empty-book" aria-hidden="true"><i></i><i></i><i></i></div>
          <h3>小说库暂时无法读取</h3>
          <p>${escapeHtml(detail)}</p>
          <button type="button" class="button button-secondary" data-retry-library>重新读取小说库</button>
        </div>`;
      $("#library-count").textContent = "读取失败";
      return;
    }
    const novels = filteredNovels();
    $("#library-count").textContent = `${novels.length} / ${state.novels.length} 部小说${selectingForProduction ? " · 点击选择" : ""}`;
    root.className = `library-content layout-${state.libraryLayout}${selectingForProduction ? " is-production-picker" : ""}`;
    if (!novels.length) {
      const hasFilters = Boolean(state.libraryQuery || state.libraryPlatformFilter || state.libraryLanguageFilter);
      root.innerHTML = `
        <div class="empty-state library-empty">
          <div class="empty-book" aria-hidden="true"><i></i><i></i><i></i></div>
          <h3>${hasFilters ? "没有符合条件的小说" : "小说库还没有内容"}</h3>
          <p>${hasFilters ? "换一个关键词，或清除平台和语种筛选。" : "添加粘贴文本、TXT 或 DOCX，建立第一部连续小说。"}</p>
          <button type="button" class="button button-secondary" ${hasFilters ? "data-clear-library-filters" : "data-open-import"}>${hasFilters ? "清除筛选" : "添加第一部小说"}</button>
        </div>`;
      return;
    }
    if (state.libraryLayout === "table") {
      root.innerHTML = `
        <div class="novel-table-wrap panel"><table class="novel-table">
          <thead><tr><th>小说</th><th>连续剧集</th><th>平台 / 口令</th><th>制作进度</th><th>更新</th></tr></thead>
          <tbody>${novels.map((novel) => {
            const bindings = novel.platform_bindings || [];
            const codeCount = bindings.reduce((sum, item) => sum + (item.codes || []).length, 0);
            const platformNames = bindings.map((item) => platformById(item.platform_id)?.name || "未知平台").join(" / ") || "待绑定";
            const progress = novel.progress || { completed: 0, total: novel.episodes?.length || 0 };
            const actionAttribute = selectingForProduction ? "data-select-production-novel" : "data-open-novel";
            return `<tr>
              <td><button type="button" class="table-novel-open" ${actionAttribute}="${escapeHtml(novel.id)}"><span class="table-cover ${coverToneClass(novel.cover_tone)} ${novel.cover_uri ? "has-cover-image" : ""}">${novel.cover_uri ? coverImageMarkup(novel) : escapeHtml(novel.title.slice(0, 1))}</span><span><b>${escapeHtml(novel.title)}</b><small class="table-novel-meta">${languageBadgeMarkup(novel)}<span>${escapeHtml((novel.tags || []).join(" · ") || "尚未添加标签")}</span></small></span></button></td>
              <td>${episodeSpineMarkup(novel, true)}</td>
              <td><b>${escapeHtml(platformNames)}</b><small>${codeCount} 个口令</small></td>
              <td><b>${progress.completed}/${progress.total}</b><small>${progress.total ? Math.round((progress.completed / progress.total) * 100) : 0}%</small></td>
              <td><small>${escapeHtml(String(novel.updated_at || "").slice(0, 10))}</small></td>
            </tr>`;
          }).join("")}</tbody>
        </table></div>`;
      return;
    }
    root.innerHTML = novels
      .map((novel) => {
        const bindings = novel.platform_bindings || [];
        const codeCount = bindings.reduce((sum, item) => sum + (item.codes || []).length, 0);
        const platformNames = bindings.map((item) => platformById(item.platform_id)?.name || "未知平台");
        const progress = novel.progress || { completed: 0, total: novel.episodes?.length || 0 };
        const hasFailed = (novel.episodes || []).some((item) => item.status === "failed");
        const actionAttribute = selectingForProduction ? "data-select-production-novel" : "data-open-novel";
        const classification = novel.story_classification || {};
        return `
          <article class="novel-card ${hasFailed ? "has-failure" : ""}">
            <button type="button" class="novel-card-open" ${actionAttribute}="${escapeHtml(novel.id)}" aria-label="${selectingForProduction ? "选择" : "打开"} ${escapeHtml(novel.title)}">
              <span class="novel-cover-art ${coverToneClass(novel.cover_tone)} ${novel.cover_uri ? "has-cover-image" : ""}">${novel.cover_uri ? coverImageMarkup(novel) : `<small>${escapeHtml(novel.tags?.[0] || "SERIAL STORY")}</small><b>${escapeHtml(novel.title)}</b><em>${escapeHtml(novelLanguageInfo(novel).label)}</em>`}</span>
              <span class="novel-card-copy">
                <span class="novel-card-meta"><span class="novel-card-kicker">${platformNames.length ? escapeHtml(platformNames.join(" / ")) : "待绑定平台"} · ${codeCount} 个口令</span>${languageBadgeMarkup(novel)}</span>
                <strong>${escapeHtml(novel.title)}</strong>
                <small>${escapeHtml((novel.tags || []).join(" · ") || "尚未添加标签")}${classification.mood ? ` · 智能分类：${escapeHtml(classification.label || storyMoodCatalog[classification.mood]?.label || classification.mood)}` : ""}</small>
              </span>
              ${episodeSpineMarkup(novel, true)}
              <span class="novel-card-foot"><span><b>${progress.completed}/${progress.total}</b> 已完成</span><span>${selectingForProduction ? "选择这部小说 →" : hasFailed ? "有失败待处理" : "查看固定资料 →"}</span></span>
            </button>
          </article>`;
      })
      .join("");
  }

  function coverImageMarkup(novel, options = {}) {
    const coverUri = String(novel?.cover_uri || "").trim();
    if (!coverUri) return "";
    const usableUri = webAssetUrl(coverUri);
    if (!usableUri) return "";
    const safeUri = escapeHtml(usableUri);
    const title = String(novel?.title || "小说").trim() || "小说";
    const alt = options.alt || `${title} 小说封面`;
    const eager = options.eager === true;
    return `<img class="cover-image-backdrop" src="${safeUri}" alt="" aria-hidden="true" /><img class="cover-image-main" src="${safeUri}" alt="${escapeHtml(alt)}" loading="${eager ? "eager" : "lazy"}" />`;
  }

  function previewCoverNovel() {
    return state.productionNovel
      || state.selectedNovel
      || state.novels.find((item) => webAssetUrl(item?.cover_uri || ""))
      || state.novels[0]
      || null;
  }

  function paintOutroCover(container, novel, animation = "gentle_push") {
    if (!container) return;
    const image = $("img", container);
    const placeholder = $(".outro-cover-placeholder", container);
    const title = String(novel?.title || "StoryForge Novel").trim() || "StoryForge Novel";
    const source = webAssetUrl(novel?.cover_uri || "");
    const motion = normalizedCoverAnimation(animation);
    container.dataset.coverAnimation = motion;
    container.classList.toggle("has-cover-image", Boolean(source));
    container.setAttribute("aria-label", `${title} 小说封面全屏结尾 · ${coverAnimationCatalog[motion].label}`);
    if (image) {
      if (source && image.getAttribute("src") !== source) image.setAttribute("src", source);
      if (!source) image.removeAttribute("src");
      image.alt = source ? `${title} 小说封面` : "";
      image.hidden = !source;
    }
    if (placeholder) {
      placeholder.hidden = Boolean(source);
      const mark = $("b", placeholder);
      const copy = $("small", placeholder);
      if (mark) mark.textContent = Array.from(title)[0] || "S";
      if (copy) copy.textContent = title.slice(0, 38);
    }
  }

  function episodeDisplayTitle(episode) {
    return String(episode?.display_title || episode?.title || `第${Number(episode?.number) || 1}集`).trim();
  }

  function episodeDurationForWpm(episode, wpm = 0) {
    const units = Math.max(0, Number(episode?.spoken_units) || 0);
    const requestedSpeed = Number(wpm) || 0;
    if (units && requestedSpeed > 0) return (units * 60) / requestedSpeed;
    const storedDuration = Math.max(0, Number(episode?.duration_seconds) || 0);
    if (storedDuration) return storedDuration;
    const defaultSpeed = Math.max(1, Number(state.settings?.narration_wpm) || 240);
    return units ? (units * 60) / defaultSpeed : 0;
  }

  function selectedEpisodeDurationSeconds(novel, episodeIds, wpm = 0) {
    const selectedIds = new Set(
      Array.from(episodeIds || [], (item) => String(item || "")).filter(Boolean),
    );
    return (novel?.episodes || [])
      .filter((episode) => selectedIds.has(String(episode?.id || "")))
      .reduce((total, episode) => total + episodeDurationForWpm(episode, wpm), 0);
  }

  function episodeMetaText(episode, wpm = 0) {
    const displayTitle = episodeDisplayTitle(episode);
    const sourceLabel = String(episode?.source_label || "").trim();
    return [sourceLabel && sourceLabel !== displayTitle ? sourceLabel : "", `预计 ${formatDuration(episodeDurationForWpm(episode, wpm))}`]
      .filter(Boolean)
      .join(" · ");
  }

  function isAuthenticatedHubBrowser() {
    return Boolean(
      isWebRuntime
      && !hasDesktopBridge()
      && state.webSession?.user
      && !state.webCapabilities?.client_local,
    );
  }

  function isClientLocalBrowser() {
    return Boolean(
      isWebRuntime
      && !hasDesktopBridge()
      && state.webSession?.user
      && state.webCapabilities?.client_local,
    );
  }

  function hasLocalMediaRuntime() {
    return Boolean(hasDesktopBridge() || isClientLocalBrowser() || state.localWorker);
  }

  function localTtsRuntimeSnapshot() {
    if (!isAuthenticatedHubBrowser()) return null;
    const runtime = state.localWorker?.runtime;
    return runtime && typeof runtime === "object" && !Array.isArray(runtime) ? runtime : null;
  }

  function effectiveTtsProviders() {
    const shared = state.settings?.providers || {};
    const local = localTtsRuntimeSnapshot();
    if (!local) return shared;
    return {
      ...shared,
      tts_provider: String(local.tts_provider || shared.tts_provider || "local_kokoro"),
      has_tts_api_key: Boolean(local.tts_api_key_configured),
      tts_endpoint: local.tts_endpoint_configured ? "configured" : "",
      kokoro_endpoint: local.kokoro_configured ? "configured" : "",
    };
  }

  function effectiveTtsSystem() {
    const shared = state.system || {};
    const local = localTtsRuntimeSnapshot();
    if (!local) return shared;
    return {
      ...shared,
      edge_tts_runtime_ready: Boolean(local.edge_tts_runtime_ready),
      embedded_kokoro_ready: Boolean(local.embedded_kokoro_ready),
    };
  }

  function effectiveMediaSystem() {
    const shared = state.system || {};
    if (!isAuthenticatedHubBrowser()) return shared;
    const local = localTtsRuntimeSnapshot();
    if (!local) {
      return {
        ...shared,
        ffmpeg_ready: false,
        ffmpeg_path: "未连接当前制作电脑",
        encoders: [],
        recommended_encoder: "",
      };
    }
    return {
      ...shared,
      ffmpeg_ready: Boolean(local.ffmpeg_ready),
      ffmpeg_path: String(local.ffmpeg_label || "当前制作电脑内置 FFmpeg"),
      encoders: Array.isArray(local.encoders) ? local.encoders : [],
      recommended_encoder: String(local.recommended_encoder || ""),
    };
  }

  function ttsProviderLabel(value) {
    const provider = String(value || "").trim().toLocaleLowerCase().replaceAll("-", "_");
    if (["edge", "edge_tts", "microsoft_edge", "microsoft_edge_tts"].includes(provider)) return "Edge TTS";
    if (["local", "kokoro", "local_kokoro", "kokoro_local", "kokoro_http", "kokoro_cli"].includes(provider)) return "Kokoro";
    if (["deepgram", "deepgram_aura", "aura", "aura_2"].includes(provider)) return "Deepgram Aura";
    return String(value || "未知服务");
  }

  function webFolderDefaults() {
    const source = state.webDefaultFolders && Object.keys(state.webDefaultFolders).length
      ? state.webDefaultFolders
      : state.web_default_folders;
    if (!source || typeof source !== "object" || Array.isArray(source)) return {};
    return Object.fromEntries(Object.keys(draftFolderCatalog).map((key) => [key, String(source[key] || "").trim()]));
  }

  function draftFolderFieldMarkup(key, draft, { hidden = false } = {}) {
    const meta = draftFolderCatalog[key];
    const id = `draft-${key.replaceAll("_", "-")}`;
    const value = String(draft[key] || "").trim();
    if (!isAuthenticatedHubBrowser() || state.localWorker) {
      return `<label class="field draft-folder-field" for="${id}" ${hidden ? "hidden" : ""}><span>${meta.label}</span><div><input id="${id}" data-draft-path="${key}" value="${escapeHtml(value)}" placeholder="${escapeHtml(meta.desktopPlaceholder)}" /><button type="button" class="button button-ghost" data-draft-folder="${key}">选择</button></div></label>`;
    }
    const helpId = `${id}-help`;
    return `<label class="field draft-folder-field draft-folder-select-field" for="${id}" ${hidden ? "hidden" : ""}>
      <span>${meta.label}<em>当前电脑</em></span>
      <div>
        <input id="${id}" data-draft-path="${key}" value="" disabled placeholder="请先启动本机制作服务" aria-describedby="${helpId}" />
        <button type="button" class="button button-ghost" data-draft-folder="${key}">重新连接本机服务</button>
        <small id="${helpId}">打开或重新启动当前电脑上的 StoryForge；Hub 只保存任务资料，素材、配音、渲染和成片都由当前员工电脑处理。</small>
      </div>
    </label>`;
  }

  function webFolderPanelMarkup() {
    if (!isAuthenticatedHubBrowser()) return "";
    const configured = Boolean(state.localWorker);
    const hostName = state.localWorker?.deviceName || "尚未连接本机制作服务";
    return `<div class="hub-folder-panel ${configured ? "" : "is-warning"}" role="note" aria-label="本机制作文件位置">
      <span class="hub-folder-panel-mark" aria-hidden="true">${configured ? "PC" : "!"}</span>
      <span class="hub-folder-panel-copy"><b>${configured ? "素材与成片只在当前制作电脑" : "本机制作服务正在等待连接"}</b><small>${configured ? "Hub 仅提供小说、口令、账号、大模型、预设与制作记录；视频、音乐、TTS、FFmpeg/GPU 和输出都使用当前电脑。" : "打开或重新启动当前电脑上的 StoryForge，本机服务会自动启动，无需管理员配置员工文件夹。"}</small></span>
      <span class="hub-folder-panel-host"><small>当前制作电脑</small><b>${escapeHtml(hostName)}</b></span>
    </div>`;
  }

  function productionPreferenceStorageKey() {
    const userKey = String(
      state.webSession?.user?.id
      || state.webSession?.user?.username
      || "local-employee",
    ).replace(/[^a-zA-Z0-9_-]/g, "_");
    return `${PRODUCTION_PREFERENCE_STORAGE_KEY}.${userKey}`;
  }

  function readProductionPreferences() {
    try {
      const parsed = JSON.parse(window.localStorage.getItem(productionPreferenceStorageKey()) || "{}");
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_error) {
      return {};
    }
  }

  function productionPreferenceSettings(settings = {}) {
    const selected = {};
    productionPreferenceSettingKeys.forEach((key) => {
      if (!Object.prototype.hasOwnProperty.call(settings, key)) return;
      try {
        selected[key] = structuredClone(settings[key]);
      } catch (_error) {
        selected[key] = JSON.parse(JSON.stringify(settings[key]));
      }
    });
    return selected;
  }

  function mergeProductionPreferenceSettings(current = {}, remembered = {}) {
    const merged = { ...current, ...remembered };
    for (const key of ["subtitle", "intro_card", "code_card", "outro_card"]) {
      if (current[key] || remembered[key]) merged[key] = { ...(current[key] || {}), ...(remembered[key] || {}) };
    }
    return merged;
  }

  function rememberedProductionContext(novelId) {
    const preferences = readProductionPreferences();
    return {
      preferences,
      novel: preferences.novels?.[String(novelId || "")] || {},
    };
  }

  function applyRememberedProductionDefaults(novel, draft) {
    if (!novel || !draft || draft._employeePreferencesApplied) return draft;
    const { preferences, novel: novelPreferences } = rememberedProductionContext(novel.id);
    const freshDraft = !String(draft.id || "").trim() || Number(draft.row_version || 0) === 0;
    if (freshDraft) {
      if (preferences.last_settings && typeof preferences.last_settings === "object") {
        draft.production_settings = mergeProductionPreferenceSettings(
          draft.production_settings || {},
          preferences.last_settings,
        );
      }
      if (Number(preferences.target_video_count) > 0) {
        draft.target_video_count = Math.max(1, Math.trunc(Number(preferences.target_video_count)));
      }
      if (novelPreferences.story_mood) draft.story_mood = String(novelPreferences.story_mood);
      if (novelPreferences.voice?.provider && novelPreferences.voice?.voice_id) {
        draft.voice = { ...novelPreferences.voice };
      }
      const rememberedPlatform = String(novelPreferences.platform_id || "");
      if (
        rememberedPlatform
        && (novel.platform_bindings || []).some((item) => item.platform_id === rememberedPlatform)
      ) {
        draft.platform_id = rememberedPlatform;
      }
      const platformKey = String(draft.platform_id || "");
      const rememberedCode = String(novelPreferences.promo_codes?.[platformKey] || "");
      if (rememberedCode) draft.promo_code_id = rememberedCode;
      const rememberedAccount = String(preferences.publishing_accounts?.[platformKey] || "");
      if (rememberedAccount) draft.publishing_account_id = rememberedAccount;
      if (novelPreferences.source_narration_audio) {
        draft.source_narration_audio = String(novelPreferences.source_narration_audio);
      }
    }
    draft._employeePreferencesApplied = true;
    return draft;
  }

  function persistProductionPreferences(novel, draft) {
    if (!novel || !draft) return;
    try {
      const preferences = readProductionPreferences();
      preferences.schema = 1;
      preferences.last_settings = productionPreferenceSettings(draft.production_settings || {});
      preferences.target_video_count = Math.max(1, Math.trunc(Number(draft.target_video_count || 1)));
      preferences.novels ||= {};
      const novelPreferences = preferences.novels[novel.id] || {};
      novelPreferences.platform_id = String(draft.platform_id || "");
      novelPreferences.story_mood = String(draft.story_mood || "suspense");
      novelPreferences.voice = draft.voice?.provider && draft.voice?.voice_id
        ? { ...draft.voice }
        : {};
      novelPreferences.source_narration_audio = String(draft.source_narration_audio || "");
      novelPreferences.promo_codes ||= {};
      const platformKey = String(draft.platform_id || "");
      if (platformKey) {
        if (draft.promo_code_id) novelPreferences.promo_codes[platformKey] = String(draft.promo_code_id);
        else delete novelPreferences.promo_codes[platformKey];
      }
      preferences.novels[novel.id] = novelPreferences;
      preferences.publishing_accounts ||= {};
      if (platformKey) {
        if (draft.publishing_account_id) preferences.publishing_accounts[platformKey] = String(draft.publishing_account_id);
        else delete preferences.publishing_accounts[platformKey];
      }
      window.localStorage.setItem(productionPreferenceStorageKey(), JSON.stringify(preferences));
    } catch (_error) {
      // Preference persistence is a convenience. The server draft remains the
      // source of truth when browser storage is unavailable.
    }
  }

  function activeDraft(novel) {
    if (!novel.draft || typeof novel.draft !== "object") novel.draft = {};
    novel.draft.platform_id ||= novel.platform_bindings?.[0]?.platform_id || "";
    novel.draft.promo_code_id ||= "";
    novel.draft.publishing_account_id ||= "";
    if (!Array.isArray(novel.draft.episode_ids)) novel.draft.episode_ids = [];
    novel.draft.variant_count = Math.max(1, Math.trunc(Number(novel.draft.variant_count) || 1));
    novel.draft.target_video_count = Math.max(
      1,
      Math.trunc(Number(novel.draft.target_video_count || novel.draft.variant_count) || 10),
    );
    novel.draft.approvals ||= { main: "pending", variants: {} };
    novel.draft.approvals.main ||= "pending";
    novel.draft.approvals.variants ||= {};
    novel.draft.video_folder ||= "";
    novel.draft.music_folder ||= "";
    novel.draft.output_folder ||= "";
    novel.draft.source_narration_audio ||= "";
    novel.draft.applied_production_preset_id ||= "";
    novel.draft.applied_production_preset_revision = Number(
      novel.draft.applied_production_preset_revision || 0,
    );
    novel.draft.applied_production_preset_hash ||= "";
    novel.draft.production_preset_dirty = Boolean(novel.draft.production_preset_dirty);
    novel.draft.intro_card_text ||= "";
    novel.draft.intro_card_source ||= "";
    if (isAuthenticatedHubBrowser() || isClientLocalBrowser()) {
      const folderDefaults = webFolderDefaults();
      Object.keys(draftFolderCatalog).forEach((key) => {
        if (folderDefaults[key]) novel.draft[key] = folderDefaults[key];
        else if (String(novel.draft[key] || "").startsWith("worker://")) novel.draft[key] = "";
      });
    }
    const suggestedMood = String(novel.story_classification?.mood || "suspense");
    novel.draft.story_mood ||= suggestedMood;
    novel.draft.story_mood_source ||= novel.draft.story_mood === suggestedMood ? "auto" : "manual";
    novel.draft.voice ||= {
      provider: novel.locked_voice_provider || "",
      voice_id: novel.locked_voice_id || "",
      label: novel.default_voice || "",
      profile: novel.locked_voice_profile || "",
    };
    const personalVisualDefaults = isEmployeeSession()
      ? state.customStylePresets.find((item) => item.id === "style-personal-default")?.settings || {}
      : {};
    const defaults = { ...(state.settings || {}), ...structuredClone(personalVisualDefaults) };
    novel.draft.production_settings ||= {
      narration_wpm: Number(defaults.narration_wpm || 240),
      bgm_volume: Number(defaults.bgm_volume || 0.28),
      adult_mode: defaults.adult_mode || "engaging",
      video_template: defaults.video_template || "classic",
      intro_card_preset: defaults.intro_card_preset || "editorial_white",
      caption_mode: defaults.caption_mode || "semantic",
      subtitle_preset: defaults.subtitle_preset || "clear_outline",
      code_card_preset: defaults.code_card_preset || "brand_pill",
      outro_card_preset: defaults.outro_card_preset || "editorial_white",
      subtitle_animation: defaults.subtitle_animation || "none",
      intro_animation: defaults.intro_animation || "fade_rise",
      preview_seconds: Number(defaults.preview_seconds || DEFAULT_PREVIEW_SECONDS),
      render_mode: defaults.render_mode || "speed",
      output_mode: "video_and_mp3",
      export_narration_audio: true,
      cover_outro_enabled: defaults.cover_outro_enabled !== false,
      cover_animation: defaults.cover_animation || "gentle_push",
      color_grade: defaults.color_grade || "neutral",
      subtitle: structuredClone(defaults.subtitle || {
        font_family: "Arial", font_size: 52, text_color: "#FFFFFF", outline_color: "#101828",
        outline_width: 4, bottom_margin: 310, horizontal_margin: 180, max_chars_per_line: 28,
      }),
      intro_card: structuredClone(defaults.intro_card || {}),
      code_card: structuredClone(defaults.code_card || {}),
      outro_card: structuredClone(defaults.outro_card || {}),
    };
    novel.draft.production_settings.video_template ||= defaults.video_template || "classic";
    novel.draft.production_settings.intro_card_preset ||= defaults.intro_card_preset || "editorial_white";
    novel.draft.production_settings.code_card_preset ||= defaults.code_card_preset || "brand_pill";
    novel.draft.production_settings.outro_card_preset ||= defaults.outro_card_preset || "editorial_white";
    novel.draft.production_settings.cover_outro_enabled = (
      novel.draft.production_settings.cover_outro_enabled !== false
    );
    novel.draft.production_settings.output_mode = normalizedProductionOutputMode(
      novel.draft.production_settings.output_mode,
    );
    novel.draft.production_settings.video_playback_speed = Math.max(
      0.8,
      Math.min(3, Number(novel.draft.production_settings.video_playback_speed || 1)),
    );
    novel.draft.production_settings.video_transition = (
      novel.draft.production_settings.video_transition === "fade" ? "fade" : "cut"
    );
    novel.draft.production_settings.bgm_mode = new Set(["auto", "manual", "none"])
      .has(String(novel.draft.production_settings.bgm_mode || ""))
      ? String(novel.draft.production_settings.bgm_mode)
      : "auto";
    novel.draft.production_settings.bgm_file ||= "";
    novel.draft.production_settings.subtitle_word_mode = new Set(["off", "cumulative", "single"])
      .has(String(novel.draft.production_settings.subtitle_word_mode || ""))
      ? String(novel.draft.production_settings.subtitle_word_mode)
      : novel.draft.production_settings.subtitle?.word_sync_enabled === true
        ? "cumulative"
        : "off";
    // Kept true for older hosts that still understand only this compatibility
    // flag. The explicit output_mode is the authoritative V0.4 contract.
    novel.draft.production_settings.export_narration_audio = true;
    novel.draft.production_settings.intro_animation ||= defaults.intro_animation || "fade_rise";
    novel.draft.production_settings.color_grade ||= defaults.color_grade || "neutral";
    novel.draft.production_settings.intro_card ||= structuredClone(defaults.intro_card || {});
    novel.draft.production_settings.code_card ||= structuredClone(defaults.code_card || {});
    novel.draft.production_settings.outro_card ||= structuredClone(defaults.outro_card || {});
    const savedPreviewSeconds = Number(
      novel.draft.production_settings.preview_seconds
      || defaults.preview_seconds
      || DEFAULT_PREVIEW_SECONDS,
    );
    // 30 seconds was the former built-in default, not a user-facing choice in
    // the production desk.  Reopen those legacy drafts with the concise
    // three-part review timeline requested for the current workflow.
    novel.draft.production_settings.preview_seconds = Math.max(
      12,
      savedPreviewSeconds === 30 ? DEFAULT_PREVIEW_SECONDS : savedPreviewSeconds,
    );
    novel.draft.production_settings.output_fps = [30, 60].includes(Number(novel.draft.production_settings.output_fps))
      ? Number(novel.draft.production_settings.output_fps)
      : Number(defaults.output_fps || 60);
    return applyRememberedProductionDefaults(novel, novel.draft);
  }

  function freshProductionDraftForNextBatch(novel, queuedDraft = null) {
    const previous = queuedDraft && typeof queuedDraft === "object"
      ? structuredClone(queuedDraft)
      : structuredClone(activeDraft(novel));
    const next = {
      ...previous,
      id: "",
      row_version: 0,
      status: "draft",
      // Start from a clean batch identity, then activeDraft restores the
      // employee's remembered choices. Local folders continue to come from
      // this workstation rather than shared preference storage.
      promo_code_id: "",
      publishing_account_id: "",
      episode_ids: [],
      source_narration_audio: "",
      intro_card_text: "",
      intro_card_source: "",
      intro_card_copies: {},
      variant_count: 1,
      approvals: { main: "pending", variants: {} },
      recipe_dirty: false,
      _employeePreferencesApplied: false,
    };
    novel.draft = next;
    return activeDraft(novel);
  }

  function beginNextProductionBatch(
    novel,
    queuedDraft = null,
    { render = true, focus = true } = {},
  ) {
    const latest = state.productionNovel?.id === novel?.id
      ? state.productionNovel
      : state.novels.find((item) => item.id === novel?.id) || novel;
    // Never turn the object returned for the queued/saved draft back into an
    // editable draft. The queue owns that frozen object; the editor receives
    // a detached novel and a detached draft for the next batch.
    const editableNovel = structuredClone(latest);
    const next = freshProductionDraftForNextBatch(editableNovel, queuedDraft);
    state.selectedProductionPresetId = String(next.applied_production_preset_id || "");
    upsertNovel(editableNovel);
    if (render) renderProductionWorkbench();
    if (focus) window.setTimeout(() => $("#production-code-select")?.focus(), 80);
    return next;
  }

  function productionPreviewSeconds(draft = null) {
    const raw = draft?.production_settings?.preview_seconds
      ?? state.settings?.preview_seconds
      ?? DEFAULT_PREVIEW_SECONDS;
    const parsed = Math.round(Number(raw) || DEFAULT_PREVIEW_SECONDS);
    const seconds = parsed === 30 ? DEFAULT_PREVIEW_SECONDS : parsed;
    return Math.max(12, Math.min(60, seconds));
  }

  function jobPreviewSeconds(job, draft = null) {
    const raw = job?.settings_snapshot?.preview_seconds;
    if (Number(raw) > 0) return Math.max(12, Math.min(60, Math.round(Number(raw))));
    return productionPreviewSeconds(draft);
  }

  function detailFailureMarkup(novel) {
    const failures = state.productionRecords.filter((item) => item.novel_id === novel.id && item.status === "failed");
    if (!failures.length) return "";
    return `<div class="detail-failure"><span class="failure-pulse"></span><div><b>${failures.length}个失败任务已跳过</b><small>${escapeHtml(failures[0].error || "失败原因已保留。")}</small></div><button type="button" class="text-button" data-open-view="records">查看记录</button></div>`;
  }

  function productionRoot() {
    return $("#production-workbench-content");
  }

  function setProductionLocalTab(tab, { focus = false } = {}) {
    const next = tab === "tasks" ? "tasks" : "create";
    state.productionLocalTab = next;
    $$('[data-production-local-tab]').forEach((button) => {
      const selected = button.dataset.productionLocalTab === next;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      if (selected && focus) button.focus();
    });
    $$('[data-production-local-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.productionLocalPanel !== next;
    });
    const grid = $(".studio-grid");
    if (grid) grid.dataset.productionLocalView = next;
    if (next === "tasks") setProductionPreviewDrawerOpen(false);
  }

  function setProductionPreviewDrawerOpen(open) {
    const drawer = $("#production-preview-drawer");
    const triggers = $$('[data-open-production-preview-drawer]');
    const next = Boolean(open) && state.productionLocalTab === "create";
    state.productionPreviewDrawerOpen = next;
    drawer?.classList.toggle("is-drawer-open", next);
    triggers.forEach((trigger) => trigger.setAttribute("aria-expanded", String(next)));
    document.documentElement.classList.toggle("production-preview-open", next);
    if (next) drawer?.querySelector("[data-close-production-preview-drawer]")?.focus();
    else if (open === false && document.activeElement?.closest?.("#production-preview-drawer")) triggers[0]?.focus();
  }

  function setProductionSectionExpanded(section, expanded) {
    if (!Object.prototype.hasOwnProperty.call(state.productionSectionExpanded, section)) return;
    const next = Boolean(expanded);
    state.productionSectionExpanded[section] = next;
    const sectionNode = $(`[data-production-section="${section}"]`, productionRoot());
    const toggle = $(`[data-toggle-production-section="${section}"]`, sectionNode);
    const body = $(`#production-section-${section}-body`, sectionNode);
    sectionNode?.classList.toggle("is-collapsed", !next);
    toggle?.setAttribute("aria-expanded", String(next));
    if (body) body.hidden = !next;
  }

  function productionBatchSlateMarkup(novel, draft) {
    const settings = draft.production_settings || {};
    const missing = productionMissingItems(novel, draft);
    const preset = productionPresetItems().find((item) => String(item.id) === state.selectedProductionPresetId);
    const selectedEpisodes = Array.isArray(draft.episode_ids) ? draft.episode_ids.length : 0;
    const duration = selectedEpisodeDurationSeconds(novel, draft.episode_ids || [], settings.narration_wpm || 240);
    const cover = novel.cover_uri
      ? coverImageMarkup(novel, { alt: "" })
      : `<span>${escapeHtml(novel.title.slice(0, 1) || "N")}</span>`;
    return `<section class="production-batch-slate" aria-label="当前批次摘要">
      <div class="production-batch-slate-cover ${coverToneClass(novel.cover_tone)} ${novel.cover_uri ? "has-cover-image" : ""}" aria-hidden="true">${cover}</div>
      <div class="production-batch-slate-copy" data-batch-slate="novel"><small>当前批次</small><b>${escapeHtml(novel.title)}</b><span>${selectedEpisodes} 集 · 预计 ${formatDuration(duration)}</span></div>
      <div class="production-batch-slate-meta" data-batch-slate="mode"><small>生成方式</small><b>${escapeHtml(productionModeLabel(settings.output_mode))}</b></div>
      <div class="production-batch-slate-meta" data-batch-slate="recipe"><small>制作方案</small><b>${escapeHtml(preset?.name || "本批自定义")}</b></div>
      <div class="production-batch-slate-state ${missing.length ? "is-incomplete" : "is-ready"}" data-batch-slate="missing"><i aria-hidden="true"></i><span><small>${missing.length ? `还缺 ${missing.length} 项` : "可以开始生成"}</small><b>${escapeHtml(missing.length ? missing.join("、") : "配置完整")}</b></span></div>
    </section>`;
  }

  function updateProductionBatchSlate(novel, draft, missing = productionMissingItems(novel, draft)) {
    const settings = draft.production_settings || {};
    const preset = productionPresetItems().find((item) => String(item.id) === state.selectedProductionPresetId);
    const duration = selectedEpisodeDurationSeconds(novel, draft.episode_ids || [], settings.narration_wpm || 240);
    const novelSlot = $('[data-batch-slate="novel"]');
    if (novelSlot) novelSlot.innerHTML = `<small>当前批次</small><b>${escapeHtml(novel.title)}</b><span>${draft.episode_ids?.length || 0} 集 · 预计 ${formatDuration(duration)}</span>`;
    const modeSlot = $('[data-batch-slate="mode"]');
    if (modeSlot) modeSlot.innerHTML = `<small>生成方式</small><b>${escapeHtml(productionModeLabel(settings.output_mode))}</b>`;
    const recipeSlot = $('[data-batch-slate="recipe"]');
    if (recipeSlot) recipeSlot.innerHTML = `<small>制作方案</small><b>${escapeHtml(preset?.name || "本批自定义")}</b>`;
    const missingSlot = $('[data-batch-slate="missing"]');
    if (missingSlot) {
      missingSlot.classList.toggle("is-incomplete", missing.length > 0);
      missingSlot.classList.toggle("is-ready", missing.length === 0);
      missingSlot.innerHTML = `<i aria-hidden="true"></i><span><small>${missing.length ? `还缺 ${missing.length} 项` : "可以开始生成"}</small><b>${escapeHtml(missing.length ? missing.join("、") : "配置完整")}</b></span>`;
    }
  }

  function updateProductionSectionSummaries(novel, draft) {
    const settings = draft.production_settings || {};
    const mode = normalizedProductionOutputMode(settings.output_mode);
    const audioOnly = mode === "audio_only";
    const content = $('[data-production-section-summary="content"]');
    if (content) {
      const platform = platformById(draft.platform_id)?.name || bindingFor(novel, draft.platform_id)?.platform_name || "平台待选";
      content.querySelector("b").textContent = audioOnly ? "1 份配音" : `${draft.target_video_count || 1} 个视频`;
      content.querySelector("small").textContent = `${platform} · 已选 ${draft.episode_ids?.length || 0} 集`;
    }
    const voice = $('[data-production-section-summary="voice"]');
    if (voice) {
      const label = mode === "reuse_audio" ? "已有配音" : draft.voice?.label || draft.voice?.voice_id || "女声待选";
      const bgm = settings.bgm_mode === "none" ? "无背景音乐" : settings.bgm_mode === "manual" ? "指定音乐" : "自动音乐";
      voice.querySelector("b").textContent = label;
      voice.querySelector("small").textContent = `${Number(settings.narration_wpm || 240)} WPM · ${bgm}`;
    }
    const visual = $('[data-production-section-summary="visual"]');
    if (visual) {
      const template = settings.video_template === "platform_story_card" ? "平台简介卡" : "经典模板";
      const captions = settings.subtitle_word_mode === "single" ? "单词逐个" : settings.subtitle_word_mode === "cumulative" ? "逐词变色" : "整句字幕";
      visual.querySelector("b").textContent = `${template} · ${captions}`;
      visual.querySelector("small").textContent = `${productionSpeedLabel(settings.video_playback_speed || 1)}× · ${Number(settings.output_fps || 60)} FPS`;
    }
    const output = $('[data-production-section-summary="output"]');
    if (output) {
      output.querySelector("b").textContent = audioOnly ? "纯旁白配音" : `${Number(settings.output_fps || 60)} FPS · ${draft.target_video_count || 1} 个视频`;
      output.querySelector("small").textContent = draft.video_folder ? "本机素材已选择" : audioOnly ? "无需视频素材" : "视频素材待选";
    }
  }

  function productionNovelPickerMarkup(novel = null) {
    return `<button type="button" class="production-novel-launch ${novel ? "has-selection" : ""}" data-open-production-picker>
      <span class="production-novel-launch-mark">${novel ? escapeHtml(novel.title.slice(0, 1)) : "N"}</span>
      <span><b>${novel ? "更换本批小说" : "打开小说库选择"}</b><small>${novel ? escapeHtml(novel.title) : "看封面并搜索书名、标签或口令"}</small></span>
      <em>${novel ? "更换 →" : "进入 →"}</em>
    </button>`;
  }

  function enterProductionNovelPicker() {
    state.librarySelectionMode = "production";
    navigate("library");
    renderNovelLibrary();
    window.setTimeout(() => $("#library-search")?.focus(), 80);
  }

  function leaveProductionNovelPicker({ returnToProduction = true } = {}) {
    state.librarySelectionMode = "";
    renderNovelLibrary();
    if (returnToProduction) {
      navigate("queue");
      window.setTimeout(() => $("[data-open-production-picker]")?.focus(), 80);
    }
  }

  function storyMoodOptions(selectedMood) {
    return Object.entries(storyMoodCatalog).map(([mood, item]) => (
      `<option value="${mood}" ${mood === selectedMood ? "selected" : ""}>${escapeHtml(item.label)}</option>`
    )).join("");
  }

  function storyClassificationMarkup(novel, draft) {
    const classification = novel.story_classification || {};
    const suggested = storyMoodCatalog[classification.mood] || null;
    const selected = storyMoodCatalog[draft.story_mood] || storyMoodCatalog.suspense;
    const manual = Boolean(suggested && classification.mood !== draft.story_mood);
    const source = classification.source === "ai"
      ? `${classification.provider || "文本 AI"}${classification.model ? ` · ${classification.model}` : ""}`
      : classification.source
        ? "本地智能规则"
        : "等待智能判断";
    return `<div class="story-classification-proof ${manual ? "is-manual" : ""}">
      <span class="story-classification-mark">AI</span>
      <span><b>${manual ? `已人工改为${selected.label}` : suggested ? `已自动选择${suggested.label}` : "尚未判断故事类型"}</b><small>${suggested ? `建议：${suggested.label} · ${escapeHtml(source)}` : "选择小说后会读取正文首段、中段和结尾综合判断"}</small></span>
      <button type="button" class="text-button" data-reclassify-story>${suggested ? "重新识别" : "立即识别"}</button>
    </div>`;
  }

  function productionVoiceCandidateMarkup(novel, draft) {
    const candidates = Array.isArray(novel.voice_candidates) ? novel.voice_candidates.slice(0, 3) : [];
    if (!candidates.length) {
      return '<div class="workbench-inline-empty"><b>还没有试听候选</b><small>选择故事情绪并生成该语种可用的女声，满意后本批全程使用同一声音。</small></div>';
    }
    const languageInfo = novelLanguageInfo(novel);
    const fallbackProfile = languageInfo.key === "en" ? "American female" : `${languageInfo.label}女声`;
    return candidates.map((candidate, index) => {
      const selected = candidate.provider === draft.voice?.provider && candidate.voice_id === draft.voice?.voice_id;
      const mockAudio = String(candidate.audio_uri || "").startsWith("mock://");
      const audioUri = webAssetUrl(candidate.audio_uri || candidate.audio_path || "");
      const audition = mockAudio || !audioUri
        ? `<button type="button" class="text-button" data-preview-voice-index="${index}">试听</button>`
        : `<audio controls preload="metadata" src="${escapeHtml(audioUri)}" aria-label="试听 ${escapeHtml(candidate.label || candidate.voice_id)}"></audio>`;
      return `<label class="production-voice-option ${selected ? "is-selected" : ""}">
        <input type="radio" name="production-voice" value="${index}" ${selected ? "checked" : ""} />
        <span class="production-voice-number">0${index + 1}</span>
        <span class="production-voice-copy"><b>${escapeHtml(candidate.label || candidate.voice_id)}</b><small>${escapeHtml(candidate.profile || fallbackProfile)} · ${escapeHtml(ttsProviderLabel(candidate.provider))} · ${Math.round(Number(candidate.duration_seconds || 0))}秒</small></span>
        <span class="production-voice-audition">${audition}</span>
      </label>`;
    }).join("");
  }

  function productionMissingItems(novel, draft) {
    const missing = [];
    const mode = normalizedProductionOutputMode(draft.production_settings?.output_mode);
    const audioOnly = mode === "audio_only";
    const reuseAudio = mode === "reuse_audio";
    const bgmMode = String(draft.production_settings?.bgm_mode || "auto");
    if (!draft.platform_id) missing.push("平台");
    if (!draft.promo_code_id) missing.push("口令");
    if (!draft.episode_ids?.length) missing.push("分集");
    if (!reuseAudio && (!draft.voice?.provider || !draft.voice?.voice_id)) missing.push("本批女声");
    if (reuseAudio && !String(draft.source_narration_audio || "").trim()) missing.push("已有配音");
    if (!audioOnly && !draft.video_folder) missing.push("视频素材");
    if (!audioOnly && bgmMode === "auto" && !draft.music_folder) missing.push("背景音乐库");
    if (!audioOnly && bgmMode === "manual" && !String(draft.production_settings?.bgm_file || "").trim()) missing.push("指定音乐文件");
    if (!draft.output_folder) missing.push("输出目录");
    if (!(novel.platform_bindings || []).length) missing.push("小说平台绑定");
    return [...new Set(missing)];
  }

  function productionPresetItems({ includeManaged = false } = {}) {
    const remoteItems = (Array.isArray(state.productionPresets) ? state.productionPresets : [])
      .filter((item) => {
        if (!item || item.curated || item.scope === "curated" || item.scope === "team") return false;
        if (!String(item.owner_user_id || "").trim()) return false;
        return Boolean(item.owned_by_current_user) || (includeManaged && !isEmployeeSession());
      });
    const remoteIds = new Set(remoteItems.map((item) => String(item.id || "")));
    const remoteNames = new Set(remoteItems.map((item) => String(item.name || "").trim().toLocaleLowerCase()));
    const legacyLocalItems = (Array.isArray(state.customStylePresets) ? state.customStylePresets : [])
      .filter((item) => {
        const remoteId = String(item?.production_preset_id || "");
        const name = String(item?.name || "").trim().toLocaleLowerCase();
        return item?.id && item?.name && item?.settings
          && !remoteIds.has(remoteId)
          && !remoteNames.has(name);
      })
      .map((item) => ({
        id: `local:${item.id}`,
        name: item.name,
        description: "这套方案来自旧版个人快捷预设；更新后会自动保存到当前账号。",
        owner_user_id: String(state.webSession?.user?.id || "local"),
        owner_display_name: String(state.webSession?.user?.display_name || state.webSession?.user?.username || "当前账号"),
        scope: "personal",
        editable: true,
        deletable: true,
        owned_by_current_user: true,
        legacy_local: true,
        local_style_id: item.id,
        recipe: { production_settings: structuredClone(item.settings) },
      }));
    return [...remoteItems, ...legacyLocalItems];
  }

  function productionPresetManagementItems() {
    return productionPresetItems({ includeManaged: true });
  }

  function productionPresetScopeLabel(item) {
    if (item?.owned_by_current_user) return "我的方案";
    const owner = String(item?.owner_display_name || "").trim();
    return owner ? `${owner}的方案` : "员工方案";
  }

  function productionPresetToolbarMarkup() {
    const items = productionPresetItems();
    const selected = items.find((item) => String(item.id) === state.selectedProductionPresetId) || null;
    const draft = state.productionNovel ? activeDraft(state.productionNovel) : null;
    const isApplied = Boolean(selected && draft?.applied_production_preset_id === selected.id);
    const canUpdate = Boolean(isApplied && selected?.editable);
    const canDelete = Boolean(selected?.deletable);
    const status = isApplied ? (draft?.production_preset_dirty ? "已套用 · 有调整" : "已套用") : (selected ? "待套用" : "自由配置");
    const options = items.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === state.selectedProductionPresetId ? "selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(productionPresetScopeLabel(item))}</option>`).join("");
    const ownershipHint = selected
      ? `${productionPresetScopeLabel(selected)}${selected.editable ? " · 可修改和删除" : " · 仅可套用"}`
      : "当前没有套用方案，下方所有选项均可自由配置";
    const emptyHint = !items.length
      ? `<small class="production-recipe-empty">还没有个人方案。先自由配置本批，满意后点击“另存为我的方案”。</small>`
      : "";
    return `<section class="production-recipe-bar ${items.length ? "" : "is-empty"}" aria-label="个人制作方案">
      <span class="production-recipe-icon" aria-hidden="true">P</span>
      <span class="production-recipe-copy"><b>我的制作方案 <em>${status}</em></b><small>${escapeHtml(selected?.description || "自由配置配音、字幕、卡片、画面和输出；常用组合可保存后重复使用。")}</small>${emptyHint}</span>
      <label class="field production-recipe-select"><span>选择已保存方案</span><select id="production-recipe-select"><option value="">自由配置（不套用方案）</option>${options}</select></label>
      <span class="production-recipe-actions">
        <button type="button" class="button button-primary" data-apply-production-preset ${selected ? "" : "disabled"}>套用方案</button>
        <button type="button" class="button button-secondary" data-save-production-preset ${canUpdate ? "" : "disabled"}>更新当前方案</button>
        <button type="button" class="button button-ghost" data-save-production-preset-as>另存为我的方案</button>
        <button type="button" class="text-button" data-delete-production-preset ${canDelete ? "" : "disabled"}>${canDelete ? (selected?.owned_by_current_user ? "删除我的方案" : "管理员删除") : ""}</button>
      </span>
      <small class="production-recipe-safety">${escapeHtml(ownershipHint)}。方案不会保存小说、平台、口令、发布账号、分集、已选女声或本机文件夹。</small>
    </section>`;
  }

  function productionModeRouterMarkup(settings = {}) {
    const mode = normalizedProductionOutputMode(settings.output_mode);
    const modes = [
      ["video_and_mp3", "常规视频生成", "从正文生成配音、字幕和视频，同时交付同名配音。", "视频 + 配音"],
      ["audio_only", "仅生成配音", "只生成纯旁白配音，不读取视频素材和背景音乐。", "配音"],
      ["reuse_audio", "已有配音更换素材", "复用已有配音，重新匹配视频素材并生成新画面。", "换素材"],
    ];
    return `<section class="production-mode-router" aria-labelledby="production-mode-title">
      <div class="production-mode-heading"><span aria-hidden="true">MODE</span><div><b id="production-mode-title">先选择本批制作方式</b><small>只显示当前方式需要的选项；选择会记住到当前员工下次制作。</small></div></div>
      <fieldset class="production-mode-options">
        <legend class="visually-hidden">制作方式</legend>
        ${modes.map(([value, label, copy, badge]) => `<label class="production-mode-choice">
          <input type="radio" name="production-output-mode" value="${value}" ${mode === value ? "checked" : ""} />
          <span><b>${label}</b><small>${copy}</small></span><em>${badge}</em>
        </label>`).join("")}
      </fieldset>
    </section>`;
  }

  function productionWpmControlMarkup(settings = {}) {
    const rawWpm = Math.max(200, Math.min(280, Math.round(Number(settings.narration_wpm || 240))));
    const presetWpm = PRODUCTION_WPM_PRESETS.find((item) => item === rawWpm) || 0;
    return `<div class="production-choice-control production-wpm-control">
      <div class="production-choice-heading"><span><b>旁白语速</b><small>选择后自动生成并播放 8–12 秒真实试听；不会用浏览器模拟声音冒充。</small></span><output id="production-wpm-proof">${rawWpm} WPM</output></div>
      <div class="production-chip-row" role="radiogroup" aria-label="旁白语速">
        ${PRODUCTION_WPM_PRESETS.map((wpm) => `<label><input type="radio" name="production-wpm-preset" value="${wpm}" ${presetWpm === wpm ? "checked" : ""} /><span>${PRODUCTION_WPM_LABELS[wpm]} · ${wpm}</span></label>`).join("")}
        <label><input type="radio" name="production-wpm-preset" value="custom" ${presetWpm ? "" : "checked"} /><span>自定义</span></label>
        <label class="production-custom-number ${presetWpm ? "is-disabled" : ""}"><span class="visually-hidden">自定义 WPM</span><input id="production-wpm-custom" type="number" min="200" max="280" step="1" value="${rawWpm}" ${presetWpm ? "disabled" : ""} /><em>WPM</em></label>
      </div>
      <div class="production-audio-proof" id="production-wpm-preview" data-state="idle">
        <span class="production-audio-proof-mark" aria-hidden="true">▶</span>
        <span><b>选择语速后自动试听</b><small id="production-wpm-preview-status" role="status" aria-live="polite">需要先选择一个真实女声候选。</small></span>
        <button type="button" class="button button-ghost" data-stop-wpm-preview hidden>停止试听</button>
      </div>
    </div>`;
  }

  function productionVideoSpeedMarkup(settings = {}) {
    const speed = Math.max(0.8, Math.min(3, Number(settings.video_playback_speed || 1)));
    const preset = PRODUCTION_VIDEO_SPEED_PRESETS.find((item) => Math.abs(item - speed) < 0.001);
    return `<div class="production-choice-control production-video-speed">
      <div class="production-choice-heading"><span><b>视频播放速度</b><small>固定速度作用于本批全部视频素材。</small></span><output id="production-video-speed-proof">${productionSpeedLabel(speed)}×</output></div>
      <div class="production-chip-row" role="radiogroup" aria-label="视频播放速度">
        ${PRODUCTION_VIDEO_SPEED_PRESETS.map((value) => `<label><input type="radio" name="production-video-speed-preset" value="${productionSpeedLabel(value)}" ${preset === value ? "checked" : ""} /><span>${productionSpeedLabel(value)}×</span></label>`).join("")}
        <label><input type="radio" name="production-video-speed-preset" value="custom" ${preset === undefined ? "checked" : ""} /><span>自定义</span></label>
        <label class="production-custom-number ${preset === undefined ? "" : "is-disabled"}"><span class="visually-hidden">自定义视频播放速度</span><input id="production-video-speed-custom" type="number" min="0.8" max="3.0" step="0.05" value="${speed}" ${preset === undefined ? "" : "disabled"} /><em>×</em></label>
      </div>
    </div>`;
  }

  function productionSourceAudioMarkup(draft) {
    const value = String(draft?.source_narration_audio || "");
    return `<div class="production-source-audio">
      <span class="production-source-audio-mark" aria-hidden="true">配音</span>
      <label class="field"><span>已有配音</span><div class="production-file-picker"><input id="production-source-narration-audio" value="${escapeHtml(value)}" placeholder="选择 StoryForge 输出的纯旁白配音" /><button type="button" class="button button-secondary" data-choose-production-file="source_narration_audio">选择配音</button></div><small>配音文件内含小说、口令和精确字幕索引，可复制到其他员工电脑复用；不会重新调用配音服务。</small></label>
    </div>`;
  }

  function productionBgmMarkup(settings = {}) {
    const mode = new Set(["auto", "manual", "none"]).has(settings.bgm_mode) ? settings.bgm_mode : "auto";
    const file = String(settings.bgm_file || "");
    return `<div class="production-bgm-control">
      <fieldset class="production-choice-cards"><legend>背景音乐</legend>
        <label><input type="radio" name="production-bgm-mode" value="auto" ${mode === "auto" ? "checked" : ""} /><span><b>自动匹配</b><small>按故事类型从音乐库选择</small></span></label>
        <label><input type="radio" name="production-bgm-mode" value="manual" ${mode === "manual" ? "checked" : ""} /><span><b>手动指定</b><small>本批固定使用一首音乐</small></span></label>
        <label><input type="radio" name="production-bgm-mode" value="none" ${mode === "none" ? "checked" : ""} /><span><b>不使用背景音乐</b><small>只保留旁白声音</small></span></label>
      </fieldset>
      <label class="field production-bgm-file" ${mode === "manual" ? "" : "hidden"}><span>指定音乐文件</span><div class="production-file-picker"><input id="production-bgm-file" value="${escapeHtml(file)}" placeholder="选择常见音频格式的音乐" /><button type="button" class="button button-secondary" data-choose-production-file="bgm_file">选择音乐</button></div></label>
      <label class="field production-bgm-volume" ${mode === "none" ? "hidden" : ""}><span>音乐音量：<output id="production-bgm-proof">${Math.round(Number(settings.bgm_volume || 0.28) * 100)}%</output></span><input id="production-bgm-volume" type="range" min="5" max="50" step="1" value="${Math.round(Number(settings.bgm_volume || 0.28) * 100)}" /><small>旁白出现时自动压低，不盖过人声。</small></label>
    </div>`;
  }

  function portableProductionSettings(settings) {
    const result = {};
    Object.entries(settings || {}).forEach(([key, value]) => {
      if (productionPresetSettingKeys.has(key)) result[key] = structuredClone(value);
    });
    return result;
  }

  function currentProductionPresetRecipe() {
    const novel = state.productionNovel;
    if (!novel) throw new Error("请先选择小说。 ");
    syncProductionDraftFromControls();
    const draft = activeDraft(novel);
    return {
      story_mood: draft.story_mood || "suspense",
      voice_profile: draft.voice?.profile || storyMoodCatalog[draft.story_mood]?.voice || "",
      target_video_count: Math.max(1, Number(draft.target_video_count || 1)),
      production_settings: portableProductionSettings(draft.production_settings || {}),
    };
  }

  async function refreshProductionPresets() {
    const data = await checkedCall("get_production_presets");
    state.productionPresets = Array.isArray(data?.items) ? data.items : [];
    if (!productionPresetItems().some((item) => item.id === state.selectedProductionPresetId)) {
      state.selectedProductionPresetId = "";
    }
    return state.productionPresets;
  }

  function applyProductionPreset(presetId = state.selectedProductionPresetId) {
    const novel = state.productionNovel;
    const preset = productionPresetItems().find((item) => String(item.id) === String(presetId));
    if (!novel || !preset) throw new Error("请选择要套用的制作方案。 ");
    const draft = activeDraft(novel);
    const recipe = structuredClone(preset.recipe || {});
    const incoming = portableProductionSettings(recipe.production_settings || {});
    const merged = { ...(draft.production_settings || {}), ...incoming };
    ["subtitle", "intro_card", "code_card", "outro_card"].forEach((key) => {
      const presetKey = key === "subtitle" ? "subtitle_preset" : `${key}_preset`;
      const resolved = incoming[presetKey]
        ? state.visual_style_presets?.[key]?.[incoming[presetKey]]
        : null;
      if (incoming[key] && typeof incoming[key] === "object") {
        // A complete saved customization wins over its bundled base.
        merged[key] = structuredClone(incoming[key]);
      } else if (resolved && typeof resolved === "object") {
        // Changing a preset id must also replace the previous full snapshot;
        // otherwise old font/colour values silently override the new preset.
        merged[key] = structuredClone(resolved);
      }
    });
    draft.production_settings = merged;
    if (storyMoodCatalog[recipe.story_mood]) {
      draft.story_mood = recipe.story_mood;
      draft.story_mood_source = "preset";
    }
    if (recipe.target_video_count) {
      draft.target_video_count = Math.max(
        1,
        Math.trunc(Number(recipe.target_video_count) || 1),
      );
    }
    const preferredProfile = String(recipe.voice_profile || "");
    if ((!draft.voice?.voice_id || !draft.voice?.provider) && preferredProfile) {
      const candidate = (novel.voice_candidates || []).find((item) => item.profile === preferredProfile);
      if (candidate) {
        draft.voice = {
          provider: candidate.provider,
          voice_id: candidate.voice_id,
          label: candidate.label,
          profile: candidate.profile,
        };
      }
    }
    state.selectedProductionPresetId = String(preset.id);
    draft.applied_production_preset_id = String(preset.id);
    draft.applied_production_preset_revision = Number(preset.revision || 1);
    draft.applied_production_preset_hash = String(preset.content_hash || "");
    draft.production_preset_dirty = false;
    renderProductionWorkbench();
    toast(`已套用“${preset.name}”，小说、口令、分集和本机目录保持不变。`, "info");
  }

  async function saveProductionPreset(button, { asNew = false } = {}) {
    const selected = productionPresetItems().find((item) => item.id === state.selectedProductionPresetId);
    const migrateLegacyLocal = Boolean(selected?.legacy_local);
    let name = selected?.name || "";
    if (asNew || !selected) {
      name = window.prompt("给这套制作方案起一个名称", "我的制作方案")?.trim() || "";
      if (!name) return;
    }
    const payload = {
      ...(asNew || migrateLegacyLocal ? {} : { id: selected.id }),
      name,
      description: asNew
        ? "当前账号保存的完整制作方案，可随时在制作台套用、更新或删除。"
        : String(selected.description || ""),
      recipe: currentProductionPresetRecipe(),
    };
    await withBusyButton(button, "正在保存…", async () => {
      const saved = await checkedCall("save_production_preset", payload);
      state.selectedProductionPresetId = String(saved.id || "");
      const draft = activeDraft(state.productionNovel);
      draft.applied_production_preset_id = state.selectedProductionPresetId;
      draft.applied_production_preset_revision = Number(saved.revision || 1);
      draft.applied_production_preset_hash = String(saved.content_hash || "");
      draft.production_preset_dirty = false;
      if (migrateLegacyLocal) {
        const local = state.customStylePresets.find((item) => item.id === selected.local_style_id);
        if (local) local.production_preset_id = String(saved.id || "");
        persistCustomStylePresets();
      }
      await refreshProductionPresets();
      renderProductionWorkbench();
      toast(`方案“${saved.name}”已保存为${productionPresetScopeLabel(saved)}。`, "info");
    });
  }

  async function deleteProductionPreset(button) {
    const selected = productionPresetItems().find((item) => item.id === state.selectedProductionPresetId);
    if (!selected) return;
    if (!selected.deletable) throw new Error("当前账号不能修改这套团队方案。");
    const action = selected.owned_by_current_user ? "从我的方案中删除" : "删除";
    if (!window.confirm(`确定要将“${selected.name}”${action}吗？`)) return;
    await withBusyButton(button, "正在处理…", async () => {
      if (selected.legacy_local) {
        state.customStylePresets = state.customStylePresets.filter((item) => item.id !== selected.local_style_id);
        persistCustomStylePresets();
      } else {
        await checkedCall("delete_production_preset", selected.id);
      }
      state.selectedProductionPresetId = "";
      const draft = state.productionNovel ? activeDraft(state.productionNovel) : null;
      if (draft) {
        draft.applied_production_preset_id = "";
        draft.applied_production_preset_revision = 0;
        draft.applied_production_preset_hash = "";
        draft.production_preset_dirty = false;
      }
      await refreshProductionPresets();
      renderProductionWorkbench();
      renderCustomStylePresets();
      toast("方案已删除。", "info");
    });
  }

  function renderProductionWorkbench() {
    const root = productionRoot();
    if (!root) return;
    if (state.wpmPreviewAudio || state.wpmPreviewDebounceTimer) {
      stopProductionWpmPreview({ silent: true });
    }
    const novel = state.productionNovel;
    if (!novel) {
      root.closest(".studio-grid")?.removeAttribute("data-production-mode");
      root.innerHTML = `<div class="workbench-empty">
        <span class="workbench-empty-mark">01</span>
        <div><h3>先选择本批要制作的小说</h3><p>选择后会在本页展开分集、配音、字幕、素材、音乐和输出设置。</p></div>
        ${productionNovelPickerMarkup()}
      </div>`;
      setProductionLocalTab(state.productionLocalTab);
      return;
    }
    const draft = activeDraft(novel);
    const bindings = novel.platform_bindings || [];
    if (!bindings.some((item) => item.platform_id === draft.platform_id)) {
      draft.platform_id = bindings[0]?.platform_id || "";
    }
    const binding = bindingFor(novel, draft.platform_id);
    const activeCodes = (binding?.codes || []).filter((item) => item.active);
    if (draft.promo_code_id && !activeCodes.some((item) => item.id === draft.promo_code_id)) {
      draft.promo_code_id = "";
    }
    const accounts = state.publishingAccounts.filter((item) => item.active !== false && (!draft.platform_id || item.platform_id === draft.platform_id));
    const selectedEpisodes = new Set(draft.episode_ids || []);
    const settings = draft.production_settings || {};
    const outputMode = normalizedProductionOutputMode(settings.output_mode);
    const audioOnly = outputMode === "audio_only";
    const reuseAudio = outputMode === "reuse_audio";
    const bgmMode = new Set(["auto", "manual", "none"]).has(settings.bgm_mode) ? settings.bgm_mode : "auto";
    root.closest(".studio-grid")?.setAttribute("data-production-mode", outputMode);
    const subtitle = settings.subtitle || {};
    const selectedDurationSeconds = selectedEpisodeDurationSeconds(
      novel,
      selectedEpisodes,
      settings.narration_wpm || 240,
    );
    const durationWarningVisible = selectedDurationSeconds > MERGED_DURATION_WARNING_SECONDS;
    const targetMinimum = 1;
    draft.target_video_count = Math.max(targetMinimum, Math.trunc(Number(draft.target_video_count || 10)));
    const currentCode = activeCodes.find((item) => item.id === draft.promo_code_id);
    const structureLocked = productionStructureLocked(novel, draft);
    const recentBatch = (
      !draft.id
      && state.lastQueuedBatch?.novelId === novel.id
      ? state.lastQueuedBatch
      : null
    );
    const configuredTtsProvider = String(effectiveTtsProviders().tts_provider || "local_kokoro").toLocaleLowerCase().replaceAll("-", "_");
    const productionTtsProvider = ["edge", "edge_tts", "microsoft_edge", "microsoft_edge_tts"].includes(configuredTtsProvider)
      ? "edge_tts"
      : ["local", "kokoro", "local_kokoro", "kokoro_local", "kokoro_http", "kokoro_cli"].includes(configuredTtsProvider)
        ? "local_kokoro"
        : configuredTtsProvider;
    const productionTtsProviderOptions = `
      <option value="local_kokoro" ${productionTtsProvider === "local_kokoro" ? "selected" : ""}>Kokoro 本地免费</option>
      <option value="edge_tts" ${productionTtsProvider === "edge_tts" ? "selected" : ""}>Edge TTS 多语种免费</option>
      ${!["local_kokoro", "edge_tts"].includes(productionTtsProvider) ? `<option value="${escapeHtml(productionTtsProvider)}" selected disabled>${escapeHtml(ttsProviderLabel(productionTtsProvider))}（管理员配置）</option>` : ""}`;
    const sectionOpen = state.productionSectionExpanded;
    const selectedPlatformName = platformById(draft.platform_id)?.name || binding?.platform_name || "平台待选";
    const selectedVoiceLabel = draft.voice?.label || draft.voice?.voice_id || (reuseAudio ? "已有配音" : "女声待选");
    const visualSummary = `${settings.video_template === "platform_story_card" ? "平台简介卡" : "经典模板"} · ${settings.subtitle_word_mode === "single" ? "单词逐个" : settings.subtitle_word_mode === "cumulative" ? "逐词变色" : "整句字幕"}`;
    const outputSummary = audioOnly
      ? "纯旁白配音"
      : `${Number(settings.output_fps || 60)} FPS · ${draft.target_video_count} 个视频`;
    root.innerHTML = `
      <div class="workbench-selection-bar">
        ${productionNovelPickerMarkup(novel)}
        <div class="workbench-book-proof"><span class="workbench-cover ${coverToneClass(novel.cover_tone)} ${novel.cover_uri ? "has-cover-image" : ""}">${novel.cover_uri ? coverImageMarkup(novel, { alt: `${novel.title} 制作封面` }) : escapeHtml(novel.title.slice(0, 1))}</span><span><small class="workbench-book-language">${languageBadgeMarkup(novel)}<span>${novel.episodes?.length || 0}个分集</span></small><b>${escapeHtml(novel.title)}</b></span></div>
        <button type="button" class="text-button" data-open-production-novel-profile="${escapeHtml(novel.id)}">查看固定资料</button>
      </div>
      ${productionBatchSlateMarkup(novel, draft)}
      ${workbenchLanguageNoticeMarkup(novel)}
      ${productionPresetToolbarMarkup()}
      ${productionModeRouterMarkup(settings)}
      ${recentBatch ? `<div class="workbench-next-batch-note" role="status" aria-live="polite">
        <span class="workbench-next-batch-mark" aria-hidden="true">✓</span>
        <span><b>上一批已加入队列，当前已是全新的制作批次</b><small>${Number(recentBatch.totalVideos || 0)} 个任务会继续在后台依次生成；制作选项和该小说上次使用的口令已恢复，仍请重新选择本批分集。</small></span>
        <button type="button" class="button button-ghost" data-open-production-picker>选择另一部小说</button>
      </div>` : ""}

      <section class="workbench-section workbench-content-section" data-production-section="content">
        <button type="button" class="workbench-section-toggle" data-toggle-production-section="content" aria-expanded="${sectionOpen.content ? "true" : "false"}" aria-controls="production-section-content-body">
          <span class="workbench-section-number">01</span>
          <span class="workbench-section-title"><b>选择本批内容</b><small>平台、口令、账号与分集</small></span>
          <span class="workbench-section-summary" data-production-section-summary="content"><b id="production-total-proof">${audioOnly ? "1 份配音" : `${draft.target_video_count} 个视频`}</b><small>${escapeHtml(selectedPlatformName)} · 已选 ${selectedEpisodes.size} 集</small></span>
          <i aria-hidden="true"></i>
        </button>
        <div id="production-section-content-body" class="workbench-section-body" ${sectionOpen.content ? "" : "hidden"}>
        ${structureLocked ? '<div class="workbench-lock-note"><span><b>这一批已经加入队列</b><small>已提交任务继续使用冻结配置，不会被后续调整覆盖。</small></span><button type="button" class="button button-secondary" data-start-next-production-batch>继续制作下一批</button></div>' : ""}
        <div class="workbench-field-grid workbench-content-grid">
          <label class="field"><span>小说平台</span><select id="production-platform-select" ${structureLocked ? "disabled" : ""}><option value="">${bindings.length ? "选择平台" : "资料库尚未绑定"}</option>${bindings.map((item) => `<option value="${escapeHtml(item.platform_id)}" ${item.platform_id === draft.platform_id ? "selected" : ""}>${escapeHtml(platformById(item.platform_id)?.name || item.platform_name || "未知平台")}</option>`).join("")}</select></label>
          <label class="field"><span>本批口令</span><select id="production-code-select" ${structureLocked ? "disabled" : ""}><option value="">${activeCodes.length ? "选择口令" : "没有可用口令"}</option>${activeCodes.map((code) => `<option value="${escapeHtml(code.id)}" ${code.id === draft.promo_code_id ? "selected" : ""}>${escapeHtml(code.value)}</option>`).join("")}</select><small>每批人工确认；默认沿用这台电脑上次为该小说和平台选择的口令。</small></label>
          <label class="field"><span>发布账号</span><select id="production-account-select" ${structureLocked ? "disabled" : ""}><option value="">待分配</option>${accounts.map((account) => `<option value="${escapeHtml(account.id)}" ${account.id === draft.publishing_account_id ? "selected" : ""}>${escapeHtml(account.name)}${account.handle ? ` · ${escapeHtml(account.handle)}` : ""}</option>`).join("")}</select></label>
          ${audioOnly ? "" : `<label class="field"><span>生成总视频数</span><input id="production-target-count" type="number" min="${targetMinimum}" step="1" value="${draft.target_video_count}" ${structureLocked ? "disabled" : ""} /><small>所选分集合并为一段完整正文；这里决定同一内容生成多少个素材版本，不设数量上限。</small></label>`}
        </div>
        ${bindings.length ? "" : '<div class="workbench-warning"><b>这部小说还没有平台和口令</b><span>请联系管理员在资料库补齐后再制作。</span><button type="button" class="text-button" data-open-view="library">去资料库</button></div>'}
        <div class="workbench-episode-head">
          <div><b>选择这次要合并制作的正文分集</b><small>可多选；所选分集按正文顺序合成一个连续视频。所选分集之间不重复回顾；若从后续集开始，仅在整组开头回顾一次。</small></div>
          <span><strong id="production-episode-selection-proof">已选 ${selectedEpisodes.size} / ${novel.episodes?.length || 0} 集 · 预计 ${formatDuration(selectedDurationSeconds)}</strong><button type="button" class="text-button" data-episode-selection="all" ${structureLocked ? "disabled" : ""}>全选</button><button type="button" class="text-button" data-episode-selection="none" ${structureLocked ? "disabled" : ""}>清空</button></span>
        </div>
        <div class="workbench-episode-grid">${(novel.episodes || []).map((episode) => `<label class="workbench-episode ${selectedEpisodes.has(episode.id) ? "is-selected" : ""} ${structureLocked ? "is-locked" : ""}"><input type="checkbox" data-episode-id="${escapeHtml(episode.id)}" ${selectedEpisodes.has(episode.id) ? "checked" : ""} ${structureLocked ? "disabled" : ""} /><span>第${Number(episode.number) || 1}集</span><span><b>${escapeHtml(episodeDisplayTitle(episode))}</b><small>${escapeHtml(episodeMetaText(episode, settings.narration_wpm || 240))}</small></span></label>`).join("")}</div>
        <div id="production-episode-duration-warning" class="workbench-duration-warning" role="status" aria-live="polite" ${durationWarningVisible ? "" : "hidden"}>
          <span class="workbench-duration-mark" aria-hidden="true">10+</span>
          <span><b id="production-episode-duration-title">预计总时长 ${formatDuration(selectedDurationSeconds)}，超过 10 分钟</b><small>仍会合成一个连续视频；只提示，不自动拆分，也不影响提交。</small></span>
        </div>
        </div>
      </section>

      <section class="workbench-section" data-production-section="voice">
        <button type="button" class="workbench-section-toggle" data-toggle-production-section="voice" aria-expanded="${sectionOpen.voice ? "true" : "false"}" aria-controls="production-section-voice-body">
          <span class="workbench-section-number">02</span>
          <span class="workbench-section-title"><b>${reuseAudio ? "已有配音与节奏" : "配音与节奏"}</b><small>${reuseAudio ? "选择已有纯旁白配音" : "确认女声、语速与背景音乐"}</small></span>
          <span class="workbench-section-summary" data-production-section-summary="voice"><b>${escapeHtml(selectedVoiceLabel)}</b><small>${Number(settings.narration_wpm || 240)} WPM · ${bgmMode === "none" ? "无背景音乐" : bgmMode === "manual" ? "指定音乐" : "自动音乐"}</small></span>
          <i aria-hidden="true"></i>
        </button>
        <div id="production-section-voice-body" class="workbench-section-body" ${sectionOpen.voice ? "" : "hidden"}>
        <div class="workbench-section-controls production-voice-controls">${reuseAudio ? "" : `<label class="field compact-inline-field"><span>本机配音服务</span><select id="production-local-tts-provider">${productionTtsProviderOptions}</select><small>只切换当前电脑；无需管理员配置文件夹或密钥。</small></label>`}<label class="field compact-inline-field"><span>故事类型</span><select id="voice-candidate-mood">${storyMoodOptions(draft.story_mood)}</select></label></div>
        ${storyClassificationMarkup(novel, draft)}
        ${reuseAudio ? productionSourceAudioMarkup(draft) : `
          <div class="production-voice-toolbar"><button type="button" class="button button-secondary" data-generate-voices>${novel.voice_candidates?.length ? "重新生成女声候选" : "生成女声候选"}</button><span>试听后选择一个；本批所有分集保持同一声音。</span></div>
          <div class="production-voice-list">${productionVoiceCandidateMarkup(novel, draft)}</div>
          ${productionWpmControlMarkup(settings)}
        `}
        ${audioOnly ? "" : productionBgmMarkup(settings)}
        </div>
      </section>

      <section class="workbench-section" data-production-section="visual" ${audioOnly ? "hidden" : ""}>
        <button type="button" class="workbench-section-toggle" data-toggle-production-section="visual" aria-expanded="${sectionOpen.visual ? "true" : "false"}" aria-controls="production-section-visual-body">
          <span class="workbench-section-number">03</span>
          <span class="workbench-section-title"><b>字幕与画面</b><small>画面效果和阅读效果分别设置</small></span>
          <span class="workbench-section-summary" data-production-section-summary="visual"><b>${escapeHtml(visualSummary)}</b><small>${productionSpeedLabel(settings.video_playback_speed || 1)}× · ${Number(settings.output_fps || 60)} FPS</small></span>
          <i aria-hidden="true"></i>
        </button>
        <div id="production-section-visual-body" class="workbench-section-body" ${sectionOpen.visual ? "" : "hidden"}>
          <div class="batch-style-scope production-visual-toolbar"><span>仅覆盖本批</span><button type="button" class="text-button" data-generate-intro-copy>${String(draft.intro_card_source || "").endsWith("_ai") ? "重新优化简介" : "AI优化简介"}</button><button type="button" class="text-button" data-open-batch-style-studio>打开样式工作室</button></div>
          <div class="production-visual-semantics">
            <section class="production-semantic-panel" data-visual-panel="picture" aria-labelledby="production-picture-panel-title">
              <div class="production-semantic-heading"><span aria-hidden="true">▧</span><div><h4 id="production-picture-panel-title">画面效果</h4><p>模板、素材速度与封面结尾</p></div></div>
              <div class="production-template-row" data-template="${escapeHtml(settings.video_template || "classic")}">
                <label class="field"><span>视频模板</span><select id="production-video-template"><option value="classic">经典模板</option><option value="platform_story_card">平台简介卡（推荐）</option></select></label>
                <label class="field"><span>简介卡样式</span><select id="production-intro-card-preset"><option value="editorial_white">杂志白卡</option><option value="cinematic_dark">电影暗卡</option><option value="romance_soft">柔光浪漫</option><option value="minimal_clean">纯净极简</option><option value="social_post">社交帖卡</option><option value="paper_note">纸张便笺</option><option value="golden_luxe">金色质感</option><option value="suspense_red">悬疑红卡</option><option value="blue_glass">蓝色玻璃</option><option value="warm_story">暖调故事</option></select></label>
              </div>
              ${productionVideoSpeedMarkup(settings)}
              <label class="option-toggle production-cover-toggle">
                <input id="production-cover-outro-enabled" type="checkbox" ${settings.cover_outro_enabled !== false ? "checked" : ""} />
                <span><b>使用小说封面结尾</b><small>封面全屏展示，旁白和字幕继续。</small></span>
                <i aria-hidden="true"></i>
              </label>
            </section>
            <section class="production-semantic-panel" data-visual-panel="captions" aria-labelledby="production-captions-panel-title">
              <div class="production-semantic-heading"><span aria-hidden="true">Aa</span><div><h4 id="production-captions-panel-title">字幕与口令</h4><p>文字怎样出现、怎样更易读</p></div></div>
              <div class="workbench-field-grid workbench-field-grid-two production-caption-primary-grid">
                <label class="field"><span>字幕预设</span><select id="production-subtitle-preset"><option value="clear_outline">清晰描边</option><option value="cinematic_shadow">电影阴影</option><option value="clean_minimal">极简阅读</option><option value="bold_drama">强力戏剧</option><option value="reader_focus">阅读聚焦</option><option value="soft_box">柔和底板</option><option value="word_pop_sync">逐词弹出（配音同步）</option><option value="romance_glow">浪漫柔光</option><option value="suspense_noir">悬疑黑金</option><option value="confession_clean">对白清透</option><option value="golden_hook">金色钩子</option><option value="midnight_reader">午夜阅读</option><option value="minimal_bottom">底部极简</option></select></label>
                <label class="field"><span>跟随旁白</span><select id="production-subtitle-word-mode"><option value="off">普通整句</option><option value="cumulative">逐词累积变色</option><option value="single">单词逐个显示</option></select></label>
                <label class="field"><span>口令卡样式</span><select id="production-code-card-preset"><option value="brand_pill">品牌胶囊</option><option value="dark_glass">深色玻璃</option><option value="light_chip">浅色标签</option><option value="outline_only">纯描边</option><option value="warning_red">醒目红条</option><option value="golden_ticket">金色票签</option><option value="romance_blush">浪漫粉签</option><option value="minimal_dark">极简暗条</option></select></label>
                <label class="field"><span>字幕切分</span><select id="production-caption-mode"><option value="semantic">按语义停顿</option><option value="sentence">按完整句子</option></select></label>
              </div>
            </section>
          </div>
          <select id="production-outro-card-preset" hidden aria-hidden="true" tabindex="-1"><option value="editorial_white">杂志白卡</option><option value="cinematic_dark">电影暗卡</option><option value="brand_focus">品牌聚焦</option><option value="minimal_clean">纯净极简</option></select>
          <details class="workbench-advanced"><summary><span><b>高级画面与字幕设置</b><small>动画、渲染和精细排版；不确定时保留默认</small></span><em>展开</em></summary><div class="workbench-field-grid workbench-field-grid-four">
            <label class="field production-cover-motion ${settings.cover_outro_enabled === false ? "is-disabled" : ""}"><span>封面结尾动效</span><select id="production-cover-animation" ${settings.cover_outro_enabled === false ? "disabled" : ""}><option value="gentle_push">舒缓推进（推荐）</option><option value="gentle_pull">缓慢拉远</option><option value="slow_pan">横向慢移</option><option value="soft_parallax">轻柔视差</option><option value="vertical_drift">纵向漂移</option><option value="focus_reveal">聚焦揭示</option><option value="cinematic_push">电影推进</option><option value="ken_burns_left">左向运镜</option><option value="ken_burns_right">右向运镜</option><option value="soft_flash">柔光闪现</option><option value="fade">柔和淡入</option><option value="none">静态封面</option></select></label>
            <label class="field"><span>全片画面色调</span><select id="production-color-grade"><option value="neutral">自然原色</option><option value="suspense_cool">悬疑冷调</option><option value="romance_warm">浪漫暖柔</option><option value="sad_muted">悲伤低饱和</option><option value="revenge_contrast">爽文高对比</option><option value="night_lift">夜景提亮</option></select></label>
            <label class="field"><span>简介卡入场</span><select id="production-intro-animation"><option value="fade_rise">淡入上浮</option><option value="soft_scale">柔和缩放</option><option value="side_reveal">侧向揭示</option><option value="layered_story">分层故事</option><option value="paper_drop">纸张落入</option><option value="none">无动画</option></select></label>
            <label class="field"><span>字幕入场</span><select id="production-subtitle-animation"><option value="none">无动画</option><option value="fade">轻柔淡入</option><option value="soft_pop">轻微弹出</option><option value="rise">柔和上浮</option><option value="mask_reveal">遮罩揭示</option><option value="typewriter">逐字显现</option></select></label>
            <label class="field"><span>素材拼接</span><select id="production-video-transition"><option value="cut">直接切换（默认）</option><option value="fade">淡化衔接（固定 0.2 秒）</option></select></label>
            <label class="field"><span>渲染模式</span><select id="production-render-mode"><option value="speed">速度优先</option><option value="quality">质量优先</option><option value="compatibility">兼容模式</option></select></label>
            <label class="field"><span>输出帧率</span><select id="production-output-fps"><option value="60">60 FPS（推荐）</option><option value="30">30 FPS（更快）</option></select></label>
            <label class="field"><span>字体</span><select id="production-subtitle-font"><option>Arial</option><option>Segoe UI</option><option>Georgia</option><option>Bahnschrift</option></select></label>
            <label class="field"><span>字号</span><input id="production-subtitle-size" type="number" min="32" max="80" value="${Number(subtitle.font_size || 52)}" /></label>
            <label class="field"><span>每行字符</span><input id="production-subtitle-chars" type="number" min="16" max="40" value="${Number(subtitle.max_chars_per_line || 28)}" /></label>
            <label class="field"><span>底部安全距离</span><input id="production-subtitle-bottom" type="number" min="220" max="600" value="${Number(subtitle.bottom_margin || 310)}" /></label>
            <label class="field"><span>左右安全距离</span><input id="production-subtitle-horizontal" type="number" min="160" max="300" value="${Number(subtitle.horizontal_margin || 180)}" /></label>
            <label class="field color-field"><span>字幕颜色</span><input id="production-subtitle-color" type="color" value="${escapeHtml(subtitle.text_color || "#FFFFFF")}" /></label>
            <label class="field color-field"><span>描边颜色</span><input id="production-subtitle-outline" type="color" value="${escapeHtml(subtitle.outline_color || "#101828")}" /></label>
          </div></details>
        </div>
      </section>

      <section class="workbench-section" data-production-section="output">
        <button type="button" class="workbench-section-toggle" data-toggle-production-section="output" aria-expanded="${sectionOpen.output ? "true" : "false"}" aria-controls="production-section-output-body">
          <span class="workbench-section-number">04</span>
          <span class="workbench-section-title"><b>素材与输出</b><small>本机视频、音乐和成片目录</small></span>
          <span class="workbench-section-summary" data-production-section-summary="output"><b id="production-output-proof">${escapeHtml(outputSummary)}</b><small>${draft.video_folder ? "本机素材已选择" : audioOnly ? "无需视频素材" : "视频素材待选"}</small></span>
          <i aria-hidden="true"></i>
        </button>
        <div id="production-section-output-body" class="workbench-section-body" ${sectionOpen.output ? "" : "hidden"}>
        <p class="production-local-folder-note">由当前制作电脑自行选择本机文件夹；管理员无需配置，素材原声始终删除。</p>
        ${webFolderPanelMarkup()}
        <div class="draft-folder-grid workbench-folder-grid">
          ${draftFolderFieldMarkup("video_folder", draft, { hidden: audioOnly })}
          ${draftFolderFieldMarkup("music_folder", draft, { hidden: audioOnly || bgmMode !== "auto" })}
          ${draftFolderFieldMarkup("output_folder", draft)}
        </div>
        <div class="production-output-extras" aria-label="选择输出内容">
          <p class="production-audio-contract-note"><b>${productionModeLabel(outputMode)}</b> · ${audioOnly ? "只交付纯旁白配音。" : reuseAudio ? "复用已有配音并交付新 MP4；不会重新调用配音。" : "交付最终 MP4 与同名纯旁白配音。"} 字幕、日志、中间 WAV 与 JSON 不会混入员工发布文件夹。</p>
        </div>
        </div>
      </section>

      <section class="workbench-section workbench-direct-section" aria-label="确认并开始生成">
        <div class="production-action-dock">
          <div class="production-action-summary" id="production-action-summary"></div>
          <div class="production-action-dock-actions">
            <button type="button" class="button button-ghost production-action-preview production-preview-drawer-trigger" data-open-production-preview-drawer aria-controls="production-preview-drawer" aria-expanded="false">预览画面</button>
            <button type="button" class="button button-primary production-direct-button" data-queue-production-draft ${structureLocked ? "disabled" : ""}>${audioOnly ? "生成纯旁白配音" : reuseAudio ? "使用已有配音生成新视频" : "生成完整视频 + 配音"}</button>
          </div>
        </div>
      </section>`;

    $("#production-video-template").value = settings.video_template || "classic";
    $("#production-intro-card-preset").value = settings.intro_card_preset || "editorial_white";
    $("#production-subtitle-preset").value = settings.subtitle_preset || "clear_outline";
    $("#production-code-card-preset").value = settings.code_card_preset || "brand_pill";
    $("#production-outro-card-preset").value = settings.outro_card_preset || "editorial_white";
    $("#production-caption-mode").value = settings.caption_mode || "semantic";
    $("#production-subtitle-word-mode").value = settings.subtitle_word_mode || "off";
    $("#production-subtitle-animation").value = settings.subtitle_animation || "none";
    $("#production-video-transition").value = settings.video_transition === "fade" ? "fade" : "cut";
    $("#production-render-mode").value = settings.render_mode || "speed";
    $("#production-output-fps").value = String(Number(settings.output_fps || 60));
    $("#production-cover-outro-enabled").checked = settings.cover_outro_enabled !== false;
    const outputModeControl = $(`input[name="production-output-mode"][value="${outputMode}"]`);
    if (outputModeControl) outputModeControl.checked = true;
    $("#production-cover-animation").value = normalizedCoverAnimation(settings.cover_animation);
    $("#production-color-grade").value = settings.color_grade || "neutral";
    $("#production-intro-animation").value = settings.intro_animation || "fade_rise";
    $("#production-subtitle-font").value = subtitle.font_family || "Arial";
    syncProductionDraftFromControls({ render: false });
    updateProductionPreview();
    state.lastDetailJobSignature = detailJobSignature(novel);
    applyWebCapabilityHints(root);
    setProductionLocalTab(state.productionLocalTab);
    if (state.productionPreviewDrawerOpen) setProductionPreviewDrawerOpen(true);
  }

  async function selectProductionNovel(novelId) {
    if (!novelId) {
      state.productionNovelId = "";
      state.productionNovel = null;
      renderProductionWorkbench();
      return;
    }
    let novel = await checkedCall("get_novel", novelId);
    const classification = novel.story_classification || {};
    const classificationCurrent = Boolean(
      classification.mood
      && (!novel.content_hash || classification.content_hash === novel.content_hash),
    );
    if (!classificationCurrent) {
      try {
        const result = await checkedCall("classify_novel", novel.id, false);
        novel = result?.novel || novel;
      } catch (error) {
        toast(`故事类型暂未自动识别：${error.message}。你仍可手动选择。`, "error");
      }
    }
    upsertNovel(novel);
    state.productionNovelId = novel.id;
    state.productionNovel = novel;
    state.selectedProductionPresetId = String(activeDraft(novel).applied_production_preset_id || "");
    renderProductionWorkbench();
  }

  async function reclassifyProductionNovel(button) {
    const novel = state.productionNovel;
    if (!novel) return;
    const draft = activeDraft(novel);
    const previousSuggestion = String(novel.story_classification?.mood || "");
    try {
      await withBusyButton(button, "正在分析正文…", async () => {
        const result = await checkedCall("classify_novel", novel.id, true);
        const updated = result?.novel || novel;
        updated.draft = { ...(updated.draft || {}), ...structuredClone(draft) };
        upsertNovel(updated);
        state.productionNovel = updated;
        const updatedDraft = activeDraft(updated);
        if (draft.story_mood_source !== "manual" || draft.story_mood === previousSuggestion) {
          updatedDraft.story_mood = result?.classification?.mood || updated.story_classification?.mood || "suspense";
          updatedDraft.story_mood_source = "auto";
        }
        if (updatedDraft.story_mood !== draft.story_mood) {
          updated.voice_candidates = [];
          updatedDraft.voice = { provider: "", voice_id: "", label: "", profile: "" };
          markProductionRecipeDirty();
        }
        renderNovelLibrary();
        renderProductionWorkbench();
        toast(`已识别为“${storyMoodCatalog[updatedDraft.story_mood]?.label || updatedDraft.story_mood}”，并自动匹配女声、素材和音乐。`, "info");
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function selectedProductionWpm() {
    const selected = $('input[name="production-wpm-preset"]:checked', productionRoot())?.value || "custom";
    const value = selected === "custom" ? Number($("#production-wpm-custom")?.value || 220) : Number(selected);
    return Math.max(200, Math.min(280, Math.round(value || 220)));
  }

  function selectedProductionVideoSpeed() {
    const selected = $('input[name="production-video-speed-preset"]:checked', productionRoot())?.value || "custom";
    const value = selected === "custom" ? Number($("#production-video-speed-custom")?.value || 1) : Number(selected);
    return Math.max(0.8, Math.min(3, Number.isFinite(value) ? value : 1));
  }

  function updateProductionCustomChoiceState() {
    const customWpm = $('input[name="production-wpm-preset"]:checked', productionRoot())?.value === "custom";
    const wpmInput = $("#production-wpm-custom");
    if (wpmInput) {
      wpmInput.disabled = !customWpm;
      wpmInput.closest(".production-custom-number")?.classList.toggle("is-disabled", !customWpm);
    }
    const customSpeed = $('input[name="production-video-speed-preset"]:checked', productionRoot())?.value === "custom";
    const speedInput = $("#production-video-speed-custom");
    if (speedInput) {
      speedInput.disabled = !customSpeed;
      speedInput.closest(".production-custom-number")?.classList.toggle("is-disabled", !customSpeed);
    }
  }

  function setWpmPreviewStatus(stateName, title, copy, { canStop = false } = {}) {
    const proof = $("#production-wpm-preview");
    if (!proof) return;
    proof.dataset.state = stateName;
    const titleNode = $("b", proof);
    const copyNode = $("#production-wpm-preview-status", proof);
    const mark = $(".production-audio-proof-mark", proof);
    if (titleNode) titleNode.textContent = title;
    if (copyNode) copyNode.textContent = copy;
    if (mark) mark.textContent = stateName === "playing" ? "■" : stateName === "loading" ? "…" : stateName === "error" ? "!" : "▶";
    const stop = $("[data-stop-wpm-preview]", proof);
    if (stop) stop.hidden = !canStop;
  }

  function stopProductionWpmPreview({ silent = false } = {}) {
    state.wpmPreviewRequestId += 1;
    if (state.wpmPreviewDebounceTimer) window.clearTimeout(state.wpmPreviewDebounceTimer);
    state.wpmPreviewDebounceTimer = null;
    if (state.wpmPreviewStopTimer) window.clearTimeout(state.wpmPreviewStopTimer);
    state.wpmPreviewStopTimer = null;
    if (state.wpmPreviewAudio) {
      state.wpmPreviewAudio.pause();
      state.wpmPreviewAudio.currentTime = 0;
    }
    state.wpmPreviewAudio = null;
    if (!silent) setWpmPreviewStatus("idle", "试听已停止", "再次选择语速即可重新生成真实试听。");
  }

  async function previewProductionWpm(wpm) {
    const novel = state.productionNovel;
    if (!novel) return;
    const draft = activeDraft(novel);
    const voice = draft.voice || {};
    stopProductionWpmPreview({ silent: true });
    const requestId = ++state.wpmPreviewRequestId;
    if (!voice.provider || !voice.voice_id) {
      setWpmPreviewStatus("error", "还不能试听语速", "请先生成并选择一个真实女声候选。");
      return;
    }
    setWpmPreviewStatus("loading", `正在生成 ${wpm} WPM 试听`, "使用当前已选声线生成 8–12 秒真实音频，请稍候。");
    try {
      const mood = String(draft.story_mood || "suspense");
      const data = await checkedCall("generate_voice_candidates", novel.id, mood, wpm);
      if (requestId !== state.wpmPreviewRequestId) return;
      const candidates = Array.isArray(data?.candidates)
        ? data.candidates
        : Array.isArray(data?.novel?.voice_candidates)
          ? data.novel.voice_candidates
          : [];
      if (candidates.length) novel.voice_candidates = candidates;
      const candidate = data?.preview || data?.candidate || candidates.find((item) => (
        String(item.provider || "") === String(voice.provider || "")
        && String(item.voice_id || "") === String(voice.voice_id || "")
      ));
      if (!candidate) {
        setWpmPreviewStatus("error", "当前声线没有试听文件", "配音服务没有返回已选声线的真实试听，请重新生成候选后再试。");
        return;
      }
      const rawUri = String(candidate.audio_uri || candidate.audio_path || "");
      const audioUri = webAssetUrl(rawUri);
      if (!audioUri || rawUri.startsWith("mock://")) {
        setWpmPreviewStatus("error", "真实试听不可用", "当前环境没有返回真实音频；语速已保存，但不会播放模拟声音。");
        return;
      }
      const audio = new Audio(audioUri);
      state.wpmPreviewAudio = audio;
      audio.addEventListener("ended", () => {
        if (state.wpmPreviewAudio !== audio) return;
        state.wpmPreviewAudio = null;
        if (state.wpmPreviewStopTimer) window.clearTimeout(state.wpmPreviewStopTimer);
        state.wpmPreviewStopTimer = null;
        setWpmPreviewStatus("ready", `${wpm} WPM 试听完成`, "这是当前声线生成的真实音频。");
      }, { once: true });
      audio.addEventListener("error", () => {
        if (state.wpmPreviewAudio !== audio) return;
        state.wpmPreviewAudio = null;
        setWpmPreviewStatus("error", "真实试听播放失败", "请重新生成女声候选，或检查本机配音服务。");
      }, { once: true });
      await audio.play();
      if (requestId !== state.wpmPreviewRequestId) {
        audio.pause();
        return;
      }
      const duration = Math.max(0, Number(candidate.duration_seconds || 0));
      setWpmPreviewStatus(
        "playing",
        `正在试听 ${wpm} WPM`,
        `${candidate.label || voice.label || voice.voice_id} · ${duration ? `${Math.min(12, Math.round(duration))} 秒` : "最长 12 秒"}真实音频`,
        { canStop: true },
      );
      state.wpmPreviewStopTimer = window.setTimeout(() => {
        if (state.wpmPreviewAudio !== audio) return;
        audio.pause();
        state.wpmPreviewAudio = null;
        state.wpmPreviewStopTimer = null;
        setWpmPreviewStatus("ready", `${wpm} WPM 试听完成`, "已播放 12 秒真实试听。");
      }, 12000);
    } catch (error) {
      if (requestId !== state.wpmPreviewRequestId) return;
      state.wpmPreviewAudio = null;
      setWpmPreviewStatus(
        "error",
        "当前版本不能生成语速试听",
        `${error.message || "本机配音试听接口不可用。"} 语速设置仍已保存，未播放模拟声音。`,
      );
    }
  }

  function scheduleProductionWpmPreview() {
    if (state.wpmPreviewDebounceTimer) window.clearTimeout(state.wpmPreviewDebounceTimer);
    const wpm = selectedProductionWpm();
    state.wpmPreviewDebounceTimer = window.setTimeout(() => {
      state.wpmPreviewDebounceTimer = null;
      void previewProductionWpm(wpm);
    }, 320);
  }

  async function chooseProductionAudioFile(button) {
    const novel = state.productionNovel;
    if (!novel) return;
    if (isWebRuntime && !isBrowserDemo && !hasDesktopBridge()) {
      toast("浏览器不能取得本机文件路径。请在桌面版选择，或把本机音频完整路径粘贴到输入框。", "error");
      return;
    }
    try {
      await withBusyButton(button, "正在选择…", async () => {
        const value = await checkedCall("choose_file", "audio");
        if (!value) return;
        const field = String(button.dataset.chooseProductionFile || "");
        if (field === "source_narration_audio") {
          activeDraft(novel).source_narration_audio = String(value);
          const input = $("#production-source-narration-audio");
          if (input) input.value = String(value);
        } else if (field === "bgm_file") {
          activeDraft(novel).production_settings.bgm_file = String(value);
          const input = $("#production-bgm-file");
          if (input) input.value = String(value);
        }
        syncProductionDraftFromControls();
        updateProductionPreview();
      });
    } catch (error) {
      toast(error.message || "无法选择音频文件，请粘贴本机完整路径。", "error");
    }
  }

  function syncProductionDraftFromControls({ render = false } = {}) {
    const novel = state.productionNovel;
    const root = productionRoot();
    if (!novel || !root) return;
    const draft = activeDraft(novel);
    draft.platform_id = $("#production-platform-select")?.value || draft.platform_id || "";
    draft.promo_code_id = $("#production-code-select")?.value || "";
    draft.publishing_account_id = $("#production-account-select")?.value || "";
    draft.episode_ids = $$('[data-episode-id]:checked', root).map((item) => item.dataset.episodeId);
    const minimum = 1;
    draft.target_video_count = Math.max(minimum, Math.trunc(Number($("#production-target-count")?.value || draft.target_video_count || 10)));
    draft.video_folder = $("#draft-video-folder")?.value.trim() || "";
    draft.music_folder = $("#draft-music-folder")?.value.trim() || "";
    draft.output_folder = $("#draft-output-folder")?.value.trim() || "";
    if ($("#production-source-narration-audio")) {
      draft.source_narration_audio = $("#production-source-narration-audio").value.trim();
    }
    const selectedMood = $("#voice-candidate-mood")?.value || draft.story_mood || "suspense";
    const suggestedMood = String(novel.story_classification?.mood || "");
    draft.story_mood = selectedMood;
    draft.story_mood_source = suggestedMood && selectedMood === suggestedMood ? "auto" : "manual";
    const selectedVoice = $('input[name="production-voice"]:checked', root);
    if (selectedVoice) {
      const candidate = novel.voice_candidates?.[Number(selectedVoice.value)];
      if (candidate) draft.voice = { provider: candidate.provider, voice_id: candidate.voice_id, label: candidate.label, profile: candidate.profile };
    }
    const productionSettings = draft.production_settings || {};
    if ($('input[name="production-wpm-preset"]', root)) {
      productionSettings.narration_wpm = selectedProductionWpm();
    }
    productionSettings.bgm_mode = $('input[name="production-bgm-mode"]:checked', root)?.value || productionSettings.bgm_mode || "auto";
    if ($("#production-bgm-file")) productionSettings.bgm_file = $("#production-bgm-file").value.trim();
    productionSettings.bgm_volume = Number($("#production-bgm-volume")?.value || Math.round(Number(productionSettings.bgm_volume || 0.28) * 100)) / 100;
    if ($('input[name="production-video-speed-preset"]', root)) {
      productionSettings.video_playback_speed = selectedProductionVideoSpeed();
    }
    productionSettings.video_transition = $("#production-video-transition")?.value === "fade" ? "fade" : "cut";
    productionSettings.video_template = $("#production-video-template")?.value || productionSettings.video_template || "classic";
    productionSettings.intro_card_preset = $("#production-intro-card-preset")?.value || productionSettings.intro_card_preset || "editorial_white";
    productionSettings.subtitle_preset = $("#production-subtitle-preset")?.value || productionSettings.subtitle_preset || "clear_outline";
    productionSettings.code_card_preset = $("#production-code-card-preset")?.value || productionSettings.code_card_preset || "brand_pill";
    productionSettings.outro_card_preset = $("#production-outro-card-preset")?.value || productionSettings.outro_card_preset || "editorial_white";
    productionSettings.caption_mode = $("#production-caption-mode")?.value || productionSettings.caption_mode || "semantic";
    productionSettings.subtitle_word_mode = $("#production-subtitle-word-mode")?.value || productionSettings.subtitle_word_mode || "off";
    productionSettings.subtitle_animation = $("#production-subtitle-animation")?.value || productionSettings.subtitle_animation || "none";
    productionSettings.render_mode = $("#production-render-mode")?.value || productionSettings.render_mode || "speed";
    productionSettings.output_fps = Number($("#production-output-fps")?.value || productionSettings.output_fps || 60);
    productionSettings.cover_outro_enabled = $("#production-cover-outro-enabled")?.checked !== false;
    productionSettings.output_mode = normalizedProductionOutputMode(
      $('input[name="production-output-mode"]:checked', root)?.value || productionSettings.output_mode,
    );
    productionSettings.export_narration_audio = true;
    productionSettings.cover_animation = normalizedCoverAnimation($("#production-cover-animation")?.value || productionSettings.cover_animation);
    productionSettings.color_grade = $("#production-color-grade")?.value || productionSettings.color_grade || "neutral";
    productionSettings.intro_animation = $("#production-intro-animation")?.value || productionSettings.intro_animation || "fade_rise";
    const coverMotionControl = $("#production-cover-animation");
    if (coverMotionControl) {
      coverMotionControl.disabled = !productionSettings.cover_outro_enabled;
      coverMotionControl.closest(".production-cover-motion")?.classList.toggle("is-disabled", !productionSettings.cover_outro_enabled);
    }
    productionSettings.subtitle ||= {};
    Object.assign(productionSettings.subtitle, {
      font_family: $("#production-subtitle-font")?.value || productionSettings.subtitle.font_family || "Arial",
      font_size: Number($("#production-subtitle-size")?.value || productionSettings.subtitle.font_size || 52),
      max_chars_per_line: Number($("#production-subtitle-chars")?.value || productionSettings.subtitle.max_chars_per_line || 28),
      bottom_margin: Number($("#production-subtitle-bottom")?.value || productionSettings.subtitle.bottom_margin || 310),
      horizontal_margin: Number($("#production-subtitle-horizontal")?.value || productionSettings.subtitle.horizontal_margin || 180),
      text_color: $("#production-subtitle-color")?.value || productionSettings.subtitle.text_color || "#FFFFFF",
      outline_color: $("#production-subtitle-outline")?.value || productionSettings.subtitle.outline_color || "#101828",
      outline_width: Number(productionSettings.subtitle.outline_width || 4),
      word_sync_enabled: productionSettings.subtitle_word_mode !== "off",
    });
    draft.production_settings = productionSettings;
    const mode = normalizedProductionOutputMode(productionSettings.output_mode);
    const audioOnly = mode === "audio_only";
    const reuseAudio = mode === "reuse_audio";
    const videoFolderField = $('[data-draft-path="video_folder"]', root)?.closest(".draft-folder-field");
    const musicFolderField = $('[data-draft-path="music_folder"]', root)?.closest(".draft-folder-field");
    if (videoFolderField) videoFolderField.hidden = audioOnly;
    if (musicFolderField) musicFolderField.hidden = audioOnly || productionSettings.bgm_mode !== "auto";
    const missing = productionMissingItems(novel, draft);
    updateProductionBatchSlate(novel, draft, missing);
    updateProductionSectionSummaries(novel, draft);
    const structureLocked = productionStructureLocked(novel, draft);
    const summary = $("#production-action-summary");
    const deliverableLabel = audioOnly ? "配音" : reuseAudio ? "换素材视频" : "完整成品";
    const effectiveOutputCount = audioOnly ? 1 : draft.target_video_count;
    if (summary) summary.innerHTML = structureLocked
      ? `<span class="summary-status is-running">已加入队列</span><b>${draft.episode_ids.length}个分集合并 · ${effectiveOutputCount}个${deliverableLabel}</b><small>当前批次正在处理，无需额外确认。</small>`
      : missing.length
        ? `<span class="summary-status is-incomplete">还缺 ${missing.length} 项</span><b>${escapeHtml(missing.join("、"))}</b><small>补齐后即可按“${productionModeLabel(mode)}”开始。</small>`
        : `<span class="summary-status is-ready">配置完整</span><b>${draft.episode_ids.length}个分集合并 · ${effectiveOutputCount}个${deliverableLabel}</b><small>${audioOnly ? "将跳过素材、音乐、字幕与视频渲染，只生成一份可复用配音。" : reuseAudio ? "将复用已有配音，更换视频素材后生成新 MP4。" : "右侧即时预览确认后，直接生成 MP4 与同名配音。"}</small>`;
    const totalProof = $("#production-total-proof");
    if (totalProof) totalProof.textContent = audioOnly
      ? "本批生成 1 份配音"
      : `预计 ${effectiveOutputCount} 个${deliverableLabel}`;
    const episodeProof = $("#production-episode-selection-proof");
    const selectedDurationSeconds = selectedEpisodeDurationSeconds(
      novel,
      draft.episode_ids,
      productionSettings.narration_wpm,
    );
    if (episodeProof) episodeProof.textContent = `已选 ${draft.episode_ids.length} / ${novel.episodes?.length || 0} 集 · 预计 ${formatDuration(selectedDurationSeconds)}`;
    const durationWarning = $("#production-episode-duration-warning");
    if (durationWarning) {
      durationWarning.hidden = selectedDurationSeconds <= MERGED_DURATION_WARNING_SECONDS;
      const durationWarningTitle = $("#production-episode-duration-title", durationWarning);
      if (durationWarningTitle) durationWarningTitle.textContent = `预计总时长 ${formatDuration(selectedDurationSeconds)}，超过 10 分钟`;
    }
    const targetCount = $("#production-target-count");
    if (targetCount) {
      targetCount.min = String(minimum);
      targetCount.removeAttribute("max");
      targetCount.value = String(draft.target_video_count);
    }
    const wpmProof = $("#production-wpm-proof");
    if (wpmProof) wpmProof.textContent = `${productionSettings.narration_wpm} WPM`;
    const speedProof = $("#production-video-speed-proof");
    if (speedProof) speedProof.textContent = `${productionSpeedLabel(productionSettings.video_playback_speed)}×`;
    const bgmProof = $("#production-bgm-proof");
    if (bgmProof) bgmProof.textContent = `${Math.round(productionSettings.bgm_volume * 100)}%`;
    const outputProof = $("#production-output-proof");
    if (outputProof) outputProof.textContent = audioOnly
      ? "纯旁白配音 · 跳过视频渲染"
      : reuseAudio
        ? `已有配音 · ${productionSpeedLabel(productionSettings.video_playback_speed)}× · 新 MP4`
        : `1080 × 1920 · ${productionSettings.output_fps} FPS · H.264 + 同名配音`;
    const launchButton = $("[data-queue-production-draft]", root);
    if (launchButton) launchButton.textContent = audioOnly
      ? "生成纯旁白配音"
      : reuseAudio
        ? "使用已有配音生成新视频"
        : "生成完整视频 + 配音";
    $("[data-queue-production-draft]", root)?.toggleAttribute("disabled", structureLocked || missing.length > 0);
    updateProductionCustomChoiceState();
    persistProductionPreferences(novel, draft);
    if (render) renderProductionWorkbench();
  }

  function resetProductionStyleFromPreset(control) {
    const config = {
      "production-intro-card-preset": ["intro_card", "intro_card_preset"],
      "production-subtitle-preset": ["subtitle", "subtitle_preset"],
      "production-code-card-preset": ["code_card", "code_card_preset"],
      "production-outro-card-preset": ["outro_card", "outro_card_preset"],
    }[control?.id];
    if (!config || !state.productionNovel) return;
    const [kind, presetField] = config;
    const draft = activeDraft(state.productionNovel);
    const values = state.visual_style_presets?.[kind]?.[control.value];
    draft.production_settings ||= {};
    draft.production_settings[presetField] = control.value;
    if (values) {
      draft.production_settings[kind] = structuredClone(values);
      // The production page keeps a compact set of subtitle override inputs.
      // Refresh them before the generic form synchronizer runs, otherwise the
      // previous input values would immediately overwrite the selected preset
      // and make several options look identical in the live preview.
      if (kind === "subtitle") {
        assignValue("#production-subtitle-font", values.font_family || "Arial");
        assignValue("#production-subtitle-size", Number(values.font_size || 52));
        assignValue("#production-subtitle-chars", Number(values.max_chars_per_line || 28));
        assignValue("#production-subtitle-bottom", Number(values.bottom_margin || 310));
        assignValue("#production-subtitle-horizontal", Number(values.horizontal_margin || 180));
        assignValue("#production-subtitle-color", values.text_color || "#FFFFFF");
        assignValue("#production-subtitle-outline", values.outline_color || "#101828");
      }
    }
  }

  function setProductionPreviewScene(scene) {
    const next = ["intro", "subtitle", "outro"].includes(scene) ? scene : "intro";
    state.productionPreviewScene = next;
    const preview = $("#video-preview");
    if (preview) preview.dataset.previewScene = next;
    $$('[data-production-preview-scene]').forEach((button) => {
      const selected = button.dataset.productionPreviewScene === next;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
  }

  function productionPreviewSceneForControl(control) {
    const id = control?.id || "";
    const name = control?.name || "";
    if (["production-video-template", "production-intro-card-preset", "production-code-card-preset", "production-color-grade", "production-intro-animation", "production-video-transition", "production-video-speed-custom"].includes(id) || name === "production-video-speed-preset") return "intro";
    if (["production-cover-outro-enabled", "production-cover-animation", "production-outro-card-preset"].includes(id)) return "outro";
    if (id.startsWith("production-subtitle-") || id === "production-caption-mode") return "subtitle";
    return "";
  }

  const introPreviewSafeArea = Object.freeze({
    widthPercent: 65.1852,
    bottomPercent: 18.75,
    cardHeightPercent: 29.1667,
  });

  function finitePreviewNumber(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function clampPreviewNumber(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function normalizedIntroPreviewGeometry(intro = {}, code = {}) {
    const widthPercent = clampPreviewNumber(
      finitePreviewNumber(intro.width_percent, 65),
      48,
      introPreviewSafeArea.widthPercent,
    );
    const codeTop = clampPreviewNumber(finitePreviewNumber(code.position_y_percent, 9), 5, 26);
    const codeFontPixels = clampPreviewNumber(finitePreviewNumber(code.font_size, 42), 28, 64) / 4.2;
    const codePaddingPixels = Math.max(4, clampPreviewNumber(finitePreviewNumber(code.padding, 20), 8, 44) / 4.2);
    const codeHeightPercent = ((codeFontPixels * 1.1) + (codePaddingPixels * 2)) / 4.5;
    const headlinePixels = clampPreviewNumber(finitePreviewNumber(intro.headline_font_size, 66), 38, 96) / 4;
    const titleHeightPercent = clampPreviewNumber(((headlinePixels * 0.98 * 2) / 4.5), 7.25, 10.5);
    const requestedCardTop = finitePreviewNumber(intro.position_y_percent, 27);
    const titleTop = Math.max(17, codeTop + codeHeightPercent + 1.5);
    const maximumCardTop = 100 - introPreviewSafeArea.bottomPercent - introPreviewSafeArea.cardHeightPercent;
    const cardTop = clampPreviewNumber(
      Math.max(requestedCardTop, titleTop + titleHeightPercent + 1.25),
      28,
      maximumCardTop,
    );

    return {
      centerPercent: 50,
      widthPercent,
      titleTopPercent: Math.min(titleTop, cardTop - titleHeightPercent - 1.25),
      cardTopPercent: cardTop,
    };
  }

  function applyIntroPreviewGeometry(introTitle, introCard, intro = {}, code = {}) {
    const geometry = normalizedIntroPreviewGeometry(intro, code);
    if (introTitle) {
      introTitle.style.left = `${geometry.centerPercent}%`;
      introTitle.style.right = "auto";
      introTitle.style.width = `${geometry.widthPercent}%`;
      introTitle.style.maxWidth = `${introPreviewSafeArea.widthPercent}%`;
      introTitle.style.top = `${geometry.titleTopPercent}%`;
      introTitle.style.transform = "translateX(-50%)";
    }
    if (introCard) {
      introCard.style.left = `${geometry.centerPercent}%`;
      introCard.style.right = "auto";
      introCard.style.width = `${geometry.widthPercent}%`;
      introCard.style.maxWidth = `${introPreviewSafeArea.widthPercent}%`;
      introCard.style.top = `${geometry.cardTopPercent}%`;
      introCard.style.transform = "translateX(-50%)";
    }
    return geometry;
  }

  function applyProductionCardStyles(root, settings) {
    const intro = settings.intro_card || {};
    const code = settings.code_card || {};
    const outro = settings.outro_card || {};
    const introTitle = $(".story-preview-title", root);
    const introCard = $(".story-summary-card", root);
    const introBody = $(".story-summary-card p", root);
    const introLabel = $(".story-card-label", root);
    const codeCard = $(".code-card", root);
    const ticket = $(".story-card-ticket", root);
    const outroCard = $(".production-outro-preview", root);
    applyIntroPreviewGeometry(introTitle, introCard, intro, code);

    if (introTitle) {
      introTitle.style.fontFamily = intro.font_family || "Arial";
      introTitle.style.fontSize = `${Number(intro.headline_font_size || 66) / 4}px`;
      introTitle.style.color = intro.headline_color || "#FFE06A";
      introTitle.style.textAlign = intro.text_alignment || "center";
    }
    if (introCard) {
      introCard.className = `story-summary-card preset-${settings.intro_card_preset || "editorial_white"}`;
      introCard.style.padding = `${Math.max(6, Number(intro.padding || 40) / 4.2)}px`;
      introCard.style.background = colorWithOpacity(intro.background_color || "#FFFFFF", Number(intro.background_opacity ?? 0.98));
      introCard.style.borderColor = intro.border_color || "#FFFFFF";
      introCard.style.borderWidth = `${Number(intro.border_width || 2) / 3}px`;
      introCard.style.borderRadius = `${Number(intro.radius || 32) / 4.2}px`;
      introCard.style.boxShadow = `0 10px 28px rgba(15,27,54,${Number(intro.shadow_opacity ?? 0.28)})`;
      introCard.style.fontFamily = intro.font_family || "Arial";
      introCard.style.textAlign = intro.text_alignment || "center";
    }
    if (introBody) {
      introBody.style.fontSize = `${Number(intro.body_font_size || 32) / 4}px`;
      introBody.style.color = intro.body_color || "#263247";
      introBody.style.textAlign = intro.text_alignment || "center";
      introBody.style.webkitLineClamp = String(Number(intro.max_lines || 5));
    }
    if (introLabel) {
      introLabel.style.fontSize = `${Number(intro.label_font_size || 24) / 4}px`;
      introLabel.style.color = intro.label_color || "#315BD8";
      introLabel.style.textAlign = intro.text_alignment || "center";
    }
    if (codeCard) {
      codeCard.className = `code-card preset-${settings.code_card_preset || "brand_pill"}`;
      codeCard.style.fontFamily = code.font_family || "Arial";
      codeCard.style.fontSize = `${Number(code.font_size || 42) / 4.2}px`;
      codeCard.style.fontWeight = code.bold === false ? "500" : "800";
      codeCard.style.color = code.text_color || "#FFFFFF";
      codeCard.style.background = colorWithOpacity(code.background_color || "#2446C8", Number(code.opacity ?? 0.92));
      codeCard.style.top = `${Number(code.position_y_percent ?? 9)}%`;
      codeCard.style.left = `${Number(code.position_x_percent ?? 50)}%`;
      codeCard.style.width = `${Number(code.width_percent || 62)}%`;
      codeCard.style.maxWidth = "none";
      codeCard.style.padding = `${Math.max(4, Number(code.padding || 20) / 4.2)}px`;
      codeCard.style.borderRadius = `${Number(code.radius || 12) / 4.2}px`;
      codeCard.style.borderColor = code.outline_color || "#FFFFFF";
      codeCard.style.borderWidth = `${Number(code.outline_width || 1) / 3}px`;
      codeCard.style.textAlign = code.alignment || "center";
      codeCard.style.transform = "translateX(-50%)";
    }
    if (ticket) ticket.className = `story-card-ticket preset-${settings.code_card_preset || "brand_pill"}`;
    if (outroCard) {
      outroCard.className = `outro-style-preview production-outro-preview preset-${settings.outro_card_preset || "editorial_white"}`;
      outroCard.style.fontFamily = outro.font_family || "Arial";
      outroCard.style.left = `${Number(outro.position_x_percent ?? 50)}%`;
      outroCard.style.top = `${Number(outro.position_y_percent || 31)}%`;
      outroCard.style.width = `${Number(outro.width_percent || 70)}%`;
      outroCard.style.height = `${Number(outro.height_percent || 38)}%`;
      outroCard.style.padding = `${Math.max(6, Number(outro.padding || 42) / 4.2)}px`;
      outroCard.style.borderRadius = `${Number(outro.radius || 32) / 4.2}px`;
      outroCard.style.borderColor = outro.border_color || "#D0DAE7";
      outroCard.style.borderWidth = `${Number(outro.border_width || 2) / 3}px`;
      outroCard.style.background = colorWithOpacity(outro.background_color || "#FFFFFF", Number(outro.background_opacity ?? 0.98));
      outroCard.style.textAlign = outro.text_alignment || "center";
      const title = $("h3", outroCard);
      const body = $("p", outroCard);
      const codeText = $("strong", outroCard);
      if (title) { title.style.fontSize = `${Number(outro.title_font_size || 62) / 4}px`; title.style.color = outro.title_color || "#17243C"; }
      if (body) { body.style.fontSize = `${Number(outro.body_font_size || 32) / 4}px`; body.style.color = outro.body_color || "#53627A"; }
      if (codeText) { codeText.style.fontSize = `${Number(outro.code_font_size || 42) / 4}px`; codeText.style.color = outro.code_color || "#315BD8"; }
    }
  }

  function updateProductionPreview() {
    const novel = state.productionNovel;
    if (!novel) return;
    const draft = activeDraft(novel);
    const binding = bindingFor(novel, draft.platform_id);
    const platform = platformById(draft.platform_id);
    const code = binding?.codes?.find((item) => item.id === draft.promo_code_id)?.value || "123456";
    $("#preview-code").textContent = platform ? safeTemplate(platform.search_template, platform.name, code) : `Search Platform: ${code}`;
    $("#preview-platform").textContent = platform?.name || "尚未选择";
    const subtitle = $("#preview-subtitle");
    const outroSubtitle = $("#preview-outro-caption");
    const settings = draft.production_settings || {};
    const previewLanguage = novelLanguageInfo(novel).code || "en";
    const root = $("#video-preview");
    const videoTemplate = settings.video_template || "classic";
    const coverAnimation = normalizedCoverAnimation(settings.cover_animation);
    const coverOutroEnabled = settings.cover_outro_enabled !== false;
    if (root) {
      root.dataset.videoTemplate = videoTemplate;
      root.dataset.subtitlePreset = settings.subtitle_preset || "clear_outline";
      root.dataset.subtitleAnimation = settings.subtitle_animation || "none";
      root.dataset.codePreset = settings.code_card_preset || "brand_pill";
      root.dataset.introPreset = settings.intro_card_preset || "editorial_white";
      root.dataset.outroPreset = settings.outro_card_preset || "editorial_white";
      root.dataset.coverAnimation = coverAnimation;
      root.dataset.coverOutroEnabled = String(coverOutroEnabled);
      root.dataset.colorGrade = settings.color_grade || "neutral";
      root.dataset.introAnimation = settings.intro_animation || "fade_rise";
      paintOutroCover($("#preview-outro-cover"), novel, coverAnimation);
      setProductionPreviewScene(state.productionPreviewScene);
      applyProductionCardStyles(root, settings);
    }
    if ($("#preview-output")) $("#preview-output").textContent = `1080 × 1920 · ${Number(settings.output_fps || 60)} FPS`;
    if ($("#preview-template")) $("#preview-template").textContent = videoTemplate === "platform_story_card" ? "平台简介卡 · 5.5秒" : "经典模板";
    if ($("#preview-story-title")) $("#preview-story-title").textContent = String(novel.title || "STORY HOOK").toUpperCase();
    if ($("#preview-story-search")) $("#preview-story-search").textContent = `${platform?.name || "Platform"} · Search “${code}”`;
    paintPlatformLogo(
      $("#preview-story-platform-mark"),
      platform || { name: "Platform", brand_color: "#315bd8" },
    );
    if ($("#preview-story-copy")) {
      $("#preview-story-copy").textContent = storyPreviewText(novel, draft);
      $("#preview-story-copy").lang = previewLanguage;
    }
    const outroCopy = platform
      ? safeTemplate(platform.ending_template, platform.name, code)
      : "Download the novel app to discover what happened next.";
    if ($("#preview-outro-copy")) $("#preview-outro-copy").textContent = outroCopy;
    if ($("#preview-outro-code")) $("#preview-outro-code").textContent = `Search “${code}”`;
    const subtitleSettings = settings.subtitle || {};
    const legacyWordSync = settings.subtitle_preset === "word_pop_sync" || subtitleSettings.word_sync_enabled === true;
    const wordMode = new Set(["off", "cumulative", "single"]).has(settings.subtitle_word_mode)
      ? settings.subtitle_word_mode
      : legacyWordSync
        ? "cumulative"
        : "off";
    const subtitleOptions = {
      preset: settings.subtitle_preset || "clear_outline",
      animation: settings.subtitle_animation || "none",
      wordMode,
    };
    applySubtitlePreviewStyles(subtitle, root, subtitleSettings, {
      ...subtitleOptions,
      text: representativeSubtitleText(novel, draft),
    });
    if (subtitle) subtitle.lang = previewLanguage;
    const outroCaptionText = String(outroCopy).includes(code) ? outroCopy : `${outroCopy} Search ${code}.`;
    applySubtitlePreviewStyles(outroSubtitle, root, subtitleSettings, {
      ...subtitleOptions,
      text: outroCaptionText,
    });
  }

  function selectedEpisodeText(novel, draft = null) {
    const selectedIds = new Set(draft?.episode_ids || []);
    const selectedEpisodes = (novel?.episodes || []).filter((item) => selectedIds.has(item.id));
    const episodes = selectedEpisodes.length ? selectedEpisodes : (novel?.episodes?.slice(0, 1) || []);
    return episodes
      .map((episode) => String(episode?.text || "").trim())
      .filter(Boolean)
      .join("\n\n");
  }

  function storyPreviewText(novel, draft = null) {
    const frozen = String(draft?.intro_card_text || "").trim();
    if (frozen) return frozen;
    const synopsis = String(novel?.synopsis || "").trim();
    const sourceText = synopsis || selectedEpisodeText(novel, draft);
    if (!sourceText) return "The saved story synopsis or selected episode excerpt will appear here.";
    const compact = sourceText.replace(/\s+/g, " ").trim();
    if (/[\u2e80-\u9fff\u3040-\u30ff\uac00-\ud7af]/u.test(compact)) {
      const characters = Array.from(compact);
      const clipped = characters.slice(0, 70).join("");
      return characters.length > 70 ? `${clipped.replace(/[\s，。！？；：、]+$/u, "")}…` : clipped;
    }
    const words = compact.split(" ");
    const byWords = words.slice(0, 28).join(" ");
    const clipped = byWords.length > 155 ? byWords.slice(0, 155).replace(/\s+\S*$/, "") : byWords;
    return words.length > 28 || byWords.length > 155
      ? `${clipped.replace(/[\s,;:\-]+$/, "")}…`
      : clipped;
  }

  function representativeSubtitleText(novel, draft = null) {
    const source = String(selectedEpisodeText(novel, draft) || novel?.synopsis || "").trim();
    if (!source) return "The selected episode text will appear here.";
    const lines = source.split(/\r?\n/u).map((line) => line.trim()).filter(Boolean);
    const proseLine = lines.find((line) => !(
      /^(?:chapter|episode|part)\s+(?:\d+|[ivxlcdm]+)\b/iu.test(line)
      || /^第\s*[\d一二三四五六七八九十百千]+\s*[章节話话回集]/u.test(line)
    )) || lines[0] || source;
    const compact = proseLine.replace(/\s+/gu, " ").trim();
    const firstSentence = compact.match(/^.*?[.!?。！？](?:["'”’」』】）)]*)/u)?.[0] || compact;
    if (/[\u2e80-\u9fff\u3040-\u30ff\uac00-\ud7af]/u.test(firstSentence)) {
      const characters = Array.from(firstSentence);
      const clipped = characters.slice(0, 42).join("");
      return characters.length > 42 ? `${clipped.replace(/[\s，。！？；：、]+$/u, "")}…` : clipped;
    }
    const words = firstSentence.split(/\s+/u);
    const clipped = words.slice(0, 18).join(" ");
    return words.length > 18 ? `${clipped.replace(/[\s,;:\-]+$/u, "")}…` : clipped;
  }

  function jobsForDraft(novel, draft) {
    if (!draft?.id) return [];
    return state.jobs.filter(
      (job) => !job.archived && job.production_draft_id === draft.id,
    );
  }

  function productionStructureLocked(novel, draft) {
    return jobsForDraft(novel, draft).some((job) => !terminalStatuses.has(job.status));
  }

  function markProductionRecipeDirty() {
    const novel = state.productionNovel;
    if (!novel) return;
    const draft = activeDraft(novel);
    if (draft.applied_production_preset_id) draft.production_preset_dirty = true;
    if (!productionStructureLocked(novel, draft)) return;
    draft.recipe_dirty = true;
  }

  function detailJobSignature(novel = state.productionNovel || state.selectedNovel) {
    if (!novel) return "";
    return jobsForDraft(novel, activeDraft(novel)).map((job) => [
      job.id,
      job.status,
      executionStatuses.has(job.status) ? Number(job.progress || 0).toFixed(2) : "",
      job.preview_uri || "",
      Boolean(job.preview_approved),
    ].join(":" )).sort().join("|");
  }

  function renderNovelDetail() {
    const novel = state.selectedNovel;
    const root = $("#novel-detail-content");
    if (!novel || !root) return;
    const libraryDraft = activeDraft(novel);
    const libraryPlatformId = libraryDraft.platform_id || novel.platform_bindings?.[0]?.platform_id || "";
    const libraryBinding = bindingFor(novel, libraryPlatformId);
    const libraryCodes = libraryBinding?.codes || [];
    const languageInfo = novelLanguageInfo(novel);
    const selectedLanguageOverride = languageInfo.manual ? languageEditorCode(languageInfo.code) : "";
    $("#novel-detail-title").textContent = novel.title;
    root.innerHTML = `
      <section class="detail-hero library-profile-hero">
        <div class="detail-cover ${coverToneClass(novel.cover_tone)} ${novel.cover_uri ? "has-cover-image" : ""}">${novel.cover_uri ? `${coverImageMarkup(novel, { eager: true })}<span class="cover-image-label">${escapeHtml(languageInfo.label)}</span>` : `<small>${escapeHtml(novel.tags?.[0] || "SERIAL STORY")}</small><b>${escapeHtml(novel.title)}</b><em>${escapeHtml(languageInfo.label)}</em>`}</div>
        <div class="detail-hero-copy">
          <div class="detail-tags">${languageBadgeMarkup(novel, { detailed: true })}${(novel.tags || []).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}</div>
          <h3>${escapeHtml(novel.title)}</h3>
          <p>${escapeHtml(novel.synopsis || "尚未填写故事简介。")}</p>
          <dl><div><dt>正文原始分集</dt><dd>${Number(novel.source_chapters || 0)}</dd></div><div><dt>可选制作分集</dt><dd>${novel.episodes?.length || 0}</dd></div><div><dt>预计全文</dt><dd>${formatDuration(novel.estimated_duration_seconds)}</dd></div></dl>
        </div>
      </section>
      <details class="novel-meta-editor web-library-edit-only" open>
        <summary>小说固定资料</summary>
        <div class="meta-editor-grid">
          <div class="meta-cover-editor"><div class="cover-thumb ${coverToneClass(novel.cover_tone)} ${novel.cover_uri ? "has-cover-image" : ""}">${novel.cover_uri ? coverImageMarkup(novel, { alt: "当前小说封面缩略图", eager: true }) : `<span>${escapeHtml(novel.title.slice(0, 1))}</span>`}</div><div><b>小说封面</b><small>${escapeHtml(novel.cover_path ? pathLeaf(novel.cover_path) : "尚未选择图片")}</small><button type="button" class="text-button" data-choose-novel-cover>选择 JPG / PNG / WEBP</button></div></div>
          <label class="field"><span>小说标题</span><input id="detail-edit-title" value="${escapeHtml(novel.title)}" /></label>
          <div class="novel-language-editor ${languageInfo.lowConfidence ? "needs-review" : ""}">
            <div class="novel-language-result"><span class="language-result-mark">${escapeHtml(languageInfo.shortLabel)}</span><span><small>${languageInfo.manual ? "人工分类" : "自动识别结果"}</small><b>${escapeHtml(languageInfo.label)}</b><em>${escapeHtml(languageConfidenceText(languageInfo))}${languageInfo.lowConfidence ? " · 建议确认" : ""}</em></span></div>
            <label class="field"><span>手动纠正语种</span><select id="detail-language-override">${languageEditorOptions(selectedLanguageOverride)}</select><small>选择具体语种后随小说资料保存；留空继续使用自动识别。</small></label>
            <button type="button" class="button button-ghost" data-redetect-novel-language>${languageInfo.manual ? "恢复自动识别" : "重新检测正文"}</button>
          </div>
          <label class="field"><span class="field-label-actions"><span>故事简介</span><button type="button" class="text-button" data-import-synopsis>从 TXT / DOCX 导入</button></span><textarea id="detail-edit-synopsis" rows="5">${escapeHtml(novel.synopsis || "")}</textarea><small id="synopsis-import-proof">可直接编辑，或导入文档后再保存。</small></label>
          <div class="meta-editor-actions"><button type="button" class="button button-ghost" data-update-manuscript>更新正文版本</button><button type="button" class="button button-secondary" data-save-novel-metadata>保存小说资料</button></div>
        </div>
      </details>
      <section class="drawer-section library-chapter-section">
        <div class="drawer-section-head"><div><p class="section-kicker">MANUSCRIPT EPISODES</p><h3>正文分集</h3></div><span>制作时可自由多选并合成一条连续正文</span></div>
        <div class="library-chapter-list">${(novel.episodes || []).map((episode) => `<article><span>第${Number(episode.number) || 1}集</span><div><b>${escapeHtml(episodeDisplayTitle(episode))}</b><small>${escapeHtml(episodeMetaText(episode))}${episode.number > 1 ? " · 本批从此集开始时自动回顾上一集" : ""}</small></div><em>${episodeStatusLabel(episode.status)}</em></article>`).join("")}</div>
      </section>
      <section class="drawer-section binding-workbench library-binding-workbench web-platform-edit-only">
        <div class="drawer-section-head"><div><p class="section-kicker">PLATFORM &amp; CODES</p><h3>平台绑定与历史口令</h3></div><span class="limit-badge">${libraryCodes.length}/5</span></div>
        <div class="binding-row">
          <label class="field"><span>小说平台</span><select id="detail-platform-select"><option value="">选择平台</option>${state.platforms.map((platform) => `<option value="${escapeHtml(platform.id)}" ${platform.id === libraryPlatformId ? "selected" : ""}>${escapeHtml(platform.name)}</option>`).join("")}</select></label>
          <button type="button" class="button button-secondary" data-save-binding>${libraryBinding ? "查看这个平台" : "绑定平台"}</button>
        </div>
        <div class="promo-code-list" id="detail-promo-code-list">${libraryCodes.length ? libraryCodes.map((code) => `<div class="promo-code-ticket ${code.active ? "" : "is-inactive"}"><span><b>${escapeHtml(code.value)}</b><small>${code.active ? "制作台可选择" : "当前已停用"}</small></span><button type="button" class="code-state-button" data-toggle-code="${escapeHtml(code.id)}" data-next-active="${code.active ? "false" : "true"}">${code.active ? "停用" : "启用"}</button></div>`).join("") : '<p class="inline-empty">这个平台还没有口令；添加后员工才能在制作台选择。</p>'}</div>
        <div class="add-code-row"><label class="field"><span class="sr-only">新增口令</span><input id="new-promo-code" maxlength="24" placeholder="输入字母与数字口令" ${!libraryBinding || libraryCodes.length >= 5 ? "disabled" : ""} /></label><button type="button" class="button button-secondary" data-add-promo-code ${!libraryBinding || libraryCodes.length >= 5 ? "disabled" : ""}>${libraryCodes.length >= 5 ? "历史累计已达5个" : "添加口令"}</button></div>
      </section>`;
    const languageSelect = $("#detail-language-override", root);
    if (languageSelect) languageSelect.value = selectedLanguageOverride;
  }

  async function openNovelDetail(novelId, trigger = null) {
    try {
      const novel = await checkedCall("get_novel", novelId);
      upsertNovel(novel);
      state.selectedNovelId = novel.id;
      state.selectedNovel = novel;
      state.detailReturnFocus = trigger || document.activeElement;
      renderNovelDetail();
      if ($("#novel-detail-content")) $("#novel-detail-content").scrollTop = 0;
      $("#novel-drawer-scrim").classList.remove("is-hidden");
      const drawer = $("#novel-detail-drawer");
      drawer.classList.remove("is-hidden");
      drawer.setAttribute("aria-hidden", "false");
      document.body.classList.add("drawer-open");
      drawer.focus();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function closeNovelDetail() {
    state.previewTimers.forEach((timer) => window.clearInterval(timer));
    state.previewTimers.clear();
    $("#novel-drawer-scrim")?.classList.add("is-hidden");
    const drawer = $("#novel-detail-drawer");
    drawer?.classList.add("is-hidden");
    drawer?.setAttribute("aria-hidden", "true");
    document.body.classList.remove("drawer-open");
    if (state.detailReturnFocus?.isConnected) state.detailReturnFocus.focus();
  }


  function normalizedProductionRecords() {
    const queueRecords = state.jobs.map((job) => ({
      id: `queue-${job.id}`,
      novel_id: "",
      title: job.title,
      episode_label: "旧版单篇任务",
      creative_line: 1,
      status: ["awaiting_approval", "waiting_preview", "interrupted"].includes(job.status)
        ? job.status
        : job.status === "failed"
          ? "failed"
          : job.status === "completed"
            ? "completed"
            : terminalStatuses.has(job.status)
              ? job.status
              : executionStatuses.has(job.status)
                ? "active"
                : "draft",
      platform_id: job.platform_id,
      promo_code: job.code,
      publishing_account_name: "待分配",
      stage_label: statusText(job),
      progress: job.progress,
      error: job.message || "",
      output_folder: job.output_folder,
      artifact_count: Number(job.artifact_count || 0),
      materials: [],
      created_at: job.created_at || "",
    }));
    return [...state.productionRecords, ...queueRecords];
  }

  function groupedProductionTasks() {
    if (!state.productionRecordGroups?.items) return [];
    return state.productionRecordGroups.items.flatMap((novel) =>
      (novel.batches || []).flatMap((batch) =>
        (batch.tasks || []).map((task) => ({ ...task, _batch: batch, _novel: novel })),
      ),
    );
  }

  function fillRecordFacet(id, items, current, emptyLabel) {
    const select = $(id);
    if (!select) return;
    const available = Array.isArray(items) ? items : [];
    const pinned = current && !available.some((item) => String(item.id) === String(current))
      ? [{ id: current, label: `当前批次 · ${String(current).slice(0, 10)}` }]
      : [];
    select.innerHTML = `<option value="">${escapeHtml(emptyLabel)}</option>${[...pinned, ...available].map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label || item.id)}</option>`).join("")}`;
    select.value = current || "";
  }

  function updateRecordBulkBar(tasks) {
    const visibleIds = new Set(tasks.map((task) => String(task.id)));
    state.selectedRecordIds = new Set(
      [...state.selectedRecordIds].filter((recordId) => visibleIds.has(recordId)),
    );
    const selected = tasks.filter((task) => state.selectedRecordIds.has(String(task.id)));
    const count = selected.length;
    if ($("#record-selected-count")) $("#record-selected-count").textContent = `已选 ${count} 项`;
    if ($("#record-select-all")) {
      $("#record-select-all").checked = Boolean(tasks.length && count === tasks.length);
      $("#record-select-all").indeterminate = Boolean(count && count < tasks.length);
    }
    const canRetry = selected.some((task) => ["failed", "cancelled", "interrupted", "skipped"].includes(task.raw_status || task.status));
    const canCancel = selected.some((task) => task.status === "active" || ["queued", "preflight", "running"].includes(task.raw_status));
    const allTerminal = Boolean(count) && selected.every((task) => ["completed", "failed", "cancelled", "interrupted", "skipped"].includes(task.raw_status || task.status));
    if ($("#record-retry-selected")) $("#record-retry-selected").disabled = !canRetry;
    if ($("#record-cancel-selected")) $("#record-cancel-selected").disabled = !canCancel;
    if ($("#record-trash-selected")) $("#record-trash-selected").disabled = !allTerminal;
    if ($("#record-restore-selected")) $("#record-restore-selected").disabled = !count;
    if ($("#record-delete-selected")) $("#record-delete-selected").disabled = !count;
    const trashMode = Boolean(state.recordTrashFilter);
    $("#record-trash-selected")?.classList.toggle("is-hidden", trashMode);
    $("#record-restore-selected")?.classList.toggle("is-hidden", !trashMode);
    $("#record-delete-selected")?.classList.toggle("is-hidden", !trashMode);
    const permissions = new Set(state.webSession?.permissions || []);
    const canManageTrash = !state.webSession?.user || permissions.has("records.view_all") || permissions.has("hub.manage");
    $("#record-trash-selected")?.classList.toggle("web-permission-hidden", !canManageTrash);
    $("#record-restore-selected")?.classList.toggle("web-permission-hidden", !canManageTrash);
    $("#record-delete-selected")?.classList.toggle("web-permission-hidden", !canManageTrash);
  }

  function recordFailureDiagnostics(diagnostics, label = "查看制作电脑错误详情") {
    if (!diagnostics || typeof diagnostics !== "object") return "";
    const summary = String(diagnostics.summary || "").trim();
    const logTail = String(diagnostics.log_tail || "").trim();
    if (!summary && !logTail) return "";
    return `<details class="record-diagnostics"><summary>${escapeHtml(label)}</summary>${summary ? `<b>${escapeHtml(summary)}</b>` : ""}${logTail ? `<pre>${escapeHtml(logTail)}</pre>` : ""}</details>`;
  }

  function renderRecords() {
    const root = $("#record-list");
    if (!root) return;
    const grouped = state.productionRecordGroups;
    const all = grouped ? groupedProductionTasks() : normalizedProductionRecords();
    const filtered = grouped ? all : all.filter((record) => {
      if (!state.recordStatusFilter) return true;
      if (state.recordStatusFilter === "active") return record.status === "active";
      return record.status === state.recordStatusFilter;
    });
    const failed = all.filter((item) => item.status === "failed");
    const completed = all.filter((item) => item.status === "completed");
    const active = all.filter((item) => item.status === "active");
    const maxUsage = Math.max(0, ...all.flatMap((item) => (item.materials || []).map((material) => Number(material.usage_count || 0))));
    $("#record-failed-count").textContent = failed.length;
    $("#record-failed-copy").textContent = failed.length ? "已跳过，原因保留" : "暂无失败";
    $("#record-completed-count").textContent = completed.length;
    $("#record-active-count").textContent = active.length;
    $("#record-max-usage").textContent = `${maxUsage}次`;
    $("#record-usage-copy").textContent = "累计使用最高值";
    $("#record-failure-summary").classList.toggle("has-failure", failed.length > 0);
    const facets = grouped?.facets || {};
    fillRecordFacet("#record-novel-filter", facets.novels, state.recordNovelFilter, "全部小说");
    fillRecordFacet("#record-batch-filter", facets.batches, state.recordBatchFilter, "全部批次");
    fillRecordFacet("#record-member-filter", facets.members, state.recordMemberFilter, "全部成员");
    fillRecordFacet("#record-device-filter", facets.devices, state.recordDeviceFilter, "全部电脑");
    updateRecordBulkBar(filtered);
    renderLibraryFailureBanner();
    if (!filtered.length) {
      root.innerHTML = '<div class="empty-state compact-empty"><h3>这个筛选下没有记录</h3><p>切换状态可查看其他制作任务。</p></div>';
      return;
    }
    if (grouped) {
      root.innerHTML = grouped.items.map((novel) => `<section class="record-novel-group">
        <header><div><span>NOVEL</span><h3>${escapeHtml(novel.title)}</h3></div><b>${Number(novel.task_count || 0)} 个视频任务</b></header>
        <div class="record-batch-list">${(novel.batches || []).map((batch) => {
          const counts = batch.status_counts || {};
          const batchLabel = batch.label || (batch.created_at ? new Date(batch.created_at).toLocaleString() : batch.id);
          const batchOutputFolder = batchResolvedOutputFolder(batch.tasks || []);
          return `<details class="record-batch" open>
            <summary><div><b>${escapeHtml(batchLabel)}</b><small>${escapeHtml(batch.member_name || "未标成员")} · ${escapeHtml(batch.device_name || "未标电脑")}</small></div><div class="record-batch-summary-tools"><div class="record-batch-counts"><span>制作中 ${Number(counts.active || 0)}</span><span class="is-success">成功 ${Number(counts.completed || 0)}</span><span class="is-failed">失败 ${Number(counts.failed || 0)}</span><span>取消 ${Number(counts.cancelled || 0)}</span></div>${batchOutputFolder ? `<button type="button" class="record-batch-output" data-output-folder="${escapeHtml(batchOutputFolder)}">打开本批文件夹</button>` : ""}</div></summary>
            <div class="record-batch-tasks">${(batch.tasks || []).map((record) => {
              const platform = platformById(record.platform_id);
              const progress = Math.round((Number(record.progress) || 0) * 100);
              const materials = (record.materials || []).map((material) => `<span class="record-material">${escapeHtml(material.name)} · ${Number(material.usage_count || 0)}次</span>`).join("");
              const attempts = record.attempts || [];
              return `<article class="record-row is-${escapeHtml(record.status)}">
                <label class="record-select"><input type="checkbox" data-select-record="${escapeHtml(record.id)}" ${state.selectedRecordIds.has(String(record.id)) ? "checked" : ""}><span class="sr-only">选择任务</span></label>
                <span class="record-state-mark"></span>
                <div class="record-copy"><div><span>${escapeHtml(record.episode_label || "")}${record.creative_line ? ` · 视频${escapeHtml(record.creative_line)}` : ""} · 第${Number(record.current_attempt || 1)}次尝试</span><b>${escapeHtml(record.title)}</b><small>${escapeHtml(platform?.name || "平台未知")} · 口令 ${escapeHtml(record.promo_code || "未选择")} · ${escapeHtml(record.publishing_account_name || "待分配")}</small></div>${record.error ? `<p class="record-error">${escapeHtml(record.error)}</p>` : ""}${recordFailureDiagnostics(record.failure_diagnostics)}${record.cancellation_reason ? `<p class="record-cancel-reason">取消原因：${escapeHtml(record.cancellation_reason)}</p>` : ""}<div class="record-materials">${materials || "<span>暂无素材使用记录</span>"}</div>${attempts.length > 1 ? `<details class="record-attempts"><summary>查看 ${attempts.length} 次尝试</summary>${attempts.map((attempt) => `<div class="record-attempt"><p><b>第${Number(attempt.attempt_no)}次</b><span>${escapeHtml(attempt.status)} · ${escapeHtml(attempt.device_id || "未知电脑")}</span>${attempt.error_message ? `<em>${escapeHtml(attempt.error_message)}</em>` : ""}</p>${recordFailureDiagnostics(attempt.metadata?.failure_diagnostics, `第${Number(attempt.attempt_no)}次技术详情`)}</div>`).join("")}</details>` : ""}</div>
                <div class="record-progress"><span>${escapeHtml(record.stage_label || record.status)}</span><b>${progress}%</b><i><em style="width:${progress}%"></em></i></div>
                <div class="record-actions">${Number(record.artifact_count || 0) > 0 ? `<button type="button" class="text-button artifact-button" data-view-record-artifacts="${escapeHtml(record.id)}">查看文件</button>` : ""}${record.novel_id ? `<button type="button" class="text-button" data-open-record-novel="${escapeHtml(record.novel_id)}">查看小说</button>` : ""}${record.status === "completed" && record.output_folder ? `<button type="button" class="text-button" data-output-folder="${escapeHtml(record.output_folder)}">打开输出</button>` : ""}</div>
              </article>`;
            }).join("")}</div>
          </details>`;
        }).join("")}</div>
      </section>`).join("");
      applyWebCapabilityHints(root);
      return;
    }
    root.innerHTML = filtered.map((record) => {
      const platform = platformById(record.platform_id);
      const progress = Math.round((Number(record.progress) || 0) * 100);
      const materials = (record.materials || []).map((material) => `<span class="record-material">${escapeHtml(material.name)} · ${Number(material.usage_count || 0)}次</span>`).join("");
      return `<article class="record-row is-${escapeHtml(record.status)}">
        <span class="record-state-mark"></span>
        <div class="record-copy"><div><span>${escapeHtml(record.episode_label || "")}${record.creative_line ? ` · 视频${escapeHtml(record.creative_line)}` : ""}</span><b>${escapeHtml(record.title)}</b><small>${escapeHtml(platform?.name || "平台未知")} · 口令 ${escapeHtml(record.promo_code || "未选择")} · ${escapeHtml(record.publishing_account_name || "待分配")}</small></div>${record.error ? `<p class="record-error">${escapeHtml(record.error)}</p>` : ""}${recordFailureDiagnostics(record.failure_diagnostics)}<div class="record-materials">${materials || "<span>暂无素材使用记录</span>"}</div></div>
        <div class="record-progress"><span>${escapeHtml(record.stage_label || record.status)}</span><b>${progress}%</b><i><em style="width:${progress}%"></em></i></div>
        <div class="record-actions">${Number(record.artifact_count || 0) > 0 ? `<button type="button" class="text-button artifact-button" data-view-record-artifacts="${escapeHtml(record.id)}">查看文件</button>` : ""}${record.novel_id ? `<button type="button" class="text-button" data-open-record-novel="${escapeHtml(record.novel_id)}">查看小说</button>` : ""}${record.status === "completed" && record.output_folder ? `<button type="button" class="text-button" data-output-folder="${escapeHtml(record.output_folder)}">打开输出</button>` : ""}</div>
      </article>`;
    }).join("");
    applyWebCapabilityHints(root);
  }

  function artifactKindLabel(kind) {
    return {
      sample: "历史预览视频",
      video: "完整成片",
      final_video: "完整成片",
      preview_narration: "历史预览旁白",
      narration: "完整旁白",
      preview_alignment: "历史预览字幕对齐",
      alignment: "完整字幕对齐",
    }[kind] || "制作文件";
  }

  function artifactFileName(artifact) {
    const source = String(artifact.cached_path || artifact.local_path || artifact.uri || "");
    const clean = source.split(/[?#]/)[0].replaceAll("\\", "/");
    return clean.split("/").filter(Boolean).pop() || artifactKindLabel(artifact.kind);
  }

  function formatFileSize(value) {
    const bytes = Number(value || 0);
    if (!bytes) return "大小未知";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function closeRecordArtifactDialog() {
    const dialog = $("#record-artifact-dialog");
    const video = $("#record-artifact-video");
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    $$("audio", dialog || document).forEach((audio) => audio.pause());
    if (dialog?.open && dialog.close) dialog.close();
    else dialog?.removeAttribute("open");
    const returnFocus = state.recordArtifactReturnFocus;
    state.recordArtifactReturnFocus = null;
    window.setTimeout(() => returnFocus?.focus(), 0);
  }

  async function openRecordArtifacts(recordId, trigger) {
    state.recordArtifactReturnFocus = trigger || document.activeElement;
    try {
      await withBusyButton(trigger, "正在读取…", async () => {
        const data = await checkedCall("get_record_artifacts", recordId);
        const record = data?.record || {};
        const artifacts = (Array.isArray(data?.artifacts) ? data.artifacts : []).map((artifact) => ({
          ...artifact,
          _media_uri: artifactMediaSource(artifact),
          _download_uri: artifactMediaSource(artifact, "download"),
        }));
        const available = artifacts.filter((artifact) => artifact.available !== false);
        const playableSample = available.find((artifact) => artifact.kind === "sample" && artifact._media_uri)
          || available.find((artifact) => ["video", "final_video"].includes(artifact.kind) && artifact._media_uri && String(artifact.mime_type || "").startsWith("video/"));
        const sidecars = artifacts.filter((artifact) => ["preview_narration", "narration", "preview_alignment", "alignment"].includes(artifact.kind));
        const dialog = $("#record-artifact-dialog");
        const video = $("#record-artifact-video");
        const empty = $("#record-artifact-video-empty");
        const title = record.title || record.novel_title || "生产记录";
        $("#record-artifact-title").textContent = `${title} · 制作文件`;
        $("#record-artifact-count").textContent = `${available.length} 个可用文件`;
        $("#record-artifact-status").textContent = `${record.episode_label || "当前记录"} · ${record.stage_label || record.status || "制作文件"}`;
        if (playableSample) {
          video.src = playableSample._media_uri;
          video.classList.remove("is-hidden");
          empty.classList.add("is-hidden");
          video.load();
          const isHubFile = String(playableSample.local_path || "").startsWith("hub://") || Boolean(playableSample.cached_path && playableSample.cached_path !== playableSample.local_path);
          const duration = Math.round(Number(playableSample.duration_seconds || 30));
          const sourceLabel = isWebRuntime && !hasDesktopBridge() ? "Hub 安全媒体" : isHubFile ? "Hub 已缓存到本机" : "本机文件";
          $("#record-artifact-source").innerHTML = `<span>${escapeHtml(sourceLabel)} · ${duration}秒 · ${escapeHtml(playableSample.device_id || "当前设备")}</span>${playableSample._download_uri ? ` <a class="artifact-download" href="${escapeHtml(playableSample._download_uri)}" download>下载视频</a>` : ""}`;
        } else {
          video.pause();
          video.removeAttribute("src");
          video.classList.add("is-hidden");
          empty.classList.remove("is-hidden");
          $("#record-artifact-source").textContent = "视频文件暂不可用；仍可查看其他制作文件。";
        }
        $("#record-artifact-list").innerHTML = sidecars.length ? sidecars.map((artifact) => {
          const usable = artifact.available !== false;
          const duration = Number(artifact.duration_seconds || 0);
          const audio = usable && artifact._media_uri && String(artifact.mime_type || "").startsWith("audio/")
            ? `<audio controls preload="metadata" src="${escapeHtml(artifact._media_uri)}" aria-label="${escapeHtml(artifactKindLabel(artifact.kind))}"></audio>`
            : "";
          const download = usable && artifact._download_uri
            ? `<a class="artifact-download" href="${escapeHtml(artifact._download_uri)}" download>下载文件</a>`
            : "";
          return `<article class="artifact-row ${usable ? "is-available" : "is-missing"}">
            <span class="artifact-kind-mark" aria-hidden="true">${String(artifact.kind || "").includes("narration") ? "VO" : "CC"}</span>
            <div class="artifact-row-copy"><div><b>${escapeHtml(artifactKindLabel(artifact.kind))}</b><span>${usable ? "可用" : "不可用"}</span></div><small>${escapeHtml(artifactFileName(artifact))} · ${formatFileSize(artifact.size_bytes)}${duration ? ` · ${Math.round(duration)}秒` : ""}</small>${audio}${download}</div>
          </article>`;
        }).join("") : '<div class="artifact-empty"><b>暂无旁白或对齐文件</b><small>后端生成并登记后会显示在这里。</small></div>';
        if (dialog?.showModal) dialog.showModal();
        else dialog?.setAttribute("open", "");
        window.setTimeout(() => video && !video.classList.contains("is-hidden") ? video.focus() : $("[data-close-record-artifacts]", dialog)?.focus(), 0);
      });
    } catch (error) {
      state.recordArtifactReturnFocus = null;
      toast(error.message, "error");
    }
  }

  function selectedSoftwareUser() {
    return state.softwareUsers.find((item) => item.id === state.selectedSoftwareUserId) || null;
  }

  function hubManagementAccessState() {
    const status = state.hubRuntimeStatus || {};
    const rawRuntimeMode = String(status.runtime_mode || status.mode || "");
    const runtimeMode = rawRuntimeMode === "embedded" ? "local" : rawRuntimeMode;
    const configuredMode = String(state.settings?.hub?.mode || status.configured_mode || "local");
    const restartRequired = Boolean(
      status.restart_required
      || (runtimeMode && configuredMode !== runtimeMode),
    );
    const canManage = runtimeMode === "host"
      && Boolean(status.running)
      && status.status !== "offline"
      && !restartRequired;
    return { canManage, configuredMode, restartRequired, runtimeMode };
  }

  function formatDeviceCreatedAt(value) {
    if (!value) return "创建时间未知";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value).slice(0, 19).replace("T", " ");
    return parsed.toLocaleString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function permissionSelection(user, permission) {
    const overrides = user?.permission_overrides || {};
    if (!Object.prototype.hasOwnProperty.call(overrides, permission)) return "inherit";
    return overrides[permission] ? "allow" : "deny";
  }

  function updatePermissionOverrideCount() {
    const count = $$('[data-user-permission]', $("#software-user-form"))
      .filter((select) => select.value !== "inherit").length;
    const proof = $("#permission-override-count");
    if (proof) proof.textContent = count ? `${count} 项特殊设置` : "无特殊设置";
  }

  function syncSoftwareRoleCards(form) {
    const role = form?.elements?.role?.value || "producer";
    $$(".role-card", form).forEach((card) => {
      card.classList.toggle("is-selected", $("input[name='role']", card)?.value === role);
    });
    const proof = $("#software-user-role-proof");
    if (proof) {
      proof.textContent = softwareRoleLabel(role);
      proof.dataset.role = role;
    }
  }

  function setSoftwareUserEditorLocked(form, locked) {
    $$('input, select, button', form).forEach((control) => {
      control.disabled = locked;
    });
    form.classList.toggle("is-upgrade-locked", locked);
    $("#software-user-upgrade-notice")?.classList.toggle("is-hidden", !locked);
  }

  function softwareUserStatusText(user, firstUser, overrideCount) {
    if (user?.role === "supervisor") {
      return "主电脑升级完成前，此账号保持只读，避免旧主管权限在保存时丢失。";
    }
    if (user && overrideCount) return `这个账号有 ${overrideCount} 项特殊权限设置。`;
    if (user) return `当前按“${softwareRoleLabel(user.role)}”默认权限运行，无需调整。`;
    if (firstUser) return "第一个账号必须是启用的管理员账号。";
    return "新账号默认使用员工权限。";
  }

  function renderSoftwareUserEditor() {
    const form = $("#software-user-form");
    if (!form) return;
    const user = selectedSoftwareUser();
    const firstUser = !state.softwareUsers.length;
    const needsHostUpgrade = user?.role === "supervisor";
    form.elements.id.value = user?.id || "";
    form.elements.username.value = user?.username || "";
    form.elements.display_name.value = user?.display_name || "";
    form.elements.role.value = user ? normalizeSoftwareRole(user.role) : (firstUser ? "admin" : "producer");
    form.elements.active.checked = user ? user.active !== false : true;
    form.elements.initial_password.value = "";
    const passwordTitle = $("#software-user-password-title");
    const passwordLabel = $("#software-user-password-label");
    const passwordState = $("#software-user-password-state");
    if (passwordTitle) passwordTitle.textContent = user ? "账号密码" : "初始密码";
    if (passwordLabel) passwordLabel.textContent = user?.has_password ? "重置密码" : "设置初始密码";
    if (passwordState) passwordState.textContent = user
      ? user.has_password
        ? "已设置密码。留空保存不会修改；输入新密码即可重置。"
        : "尚未设置密码，该成员暂时不能用账号密码连接。"
      : "设置后，成员可直接用账号密码连接主电脑。";
    $("#software-user-editor-title").textContent = user ? `编辑 ${user.display_name || user.username}` : "新建账号";
    syncSoftwareRoleCards(form);
    const overrideCount = Object.keys(user?.permission_overrides || {}).length;
    const status = $("#software-user-status");
    status.dataset.state = needsHostUpgrade ? "warning" : "normal";
    status.textContent = softwareUserStatusText(user, firstUser, overrideCount);
    const permissionGroups = new Map();
    for (const item of softwarePermissionCatalog) {
      const group = item[3];
      if (!permissionGroups.has(group)) permissionGroups.set(group, []);
      permissionGroups.get(group).push(item);
    }
    $("#software-user-permissions").innerHTML = [...permissionGroups.entries()].map(([group, items]) => `<section class="permission-group"><h3>${escapeHtml(group)}</h3><div>${items.map(([permission, label, help]) => `<label class="permission-row"><span><b>${escapeHtml(label)}</b><small>${escapeHtml(help)}</small></span><select data-user-permission="${escapeHtml(permission)}" aria-label="${escapeHtml(label)}"><option value="inherit" ${permissionSelection(user, permission) === "inherit" ? "selected" : ""}>按账号类型默认</option><option value="allow" ${permissionSelection(user, permission) === "allow" ? "selected" : ""}>额外开放</option><option value="deny" ${permissionSelection(user, permission) === "deny" ? "selected" : ""}>禁止使用</option></select></label>`).join("")}</div></section>`).join("");
    updatePermissionOverrideCount();
    const advanced = $("#advanced-permissions");
    if (advanced) advanced.open = false;
    setSoftwareUserEditorLocked(form, needsHostUpgrade);
    const deleteButton = $("#delete-software-user");
    if (deleteButton) {
      deleteButton.classList.toggle("is-hidden", !user);
      deleteButton.disabled = !user || needsHostUpgrade;
    }
  }

  function renderSoftwareUsers() {
    const root = $("#software-user-list");
    if (!root) return;
    if (state.selectedSoftwareUserId && !state.softwareUsers.some((item) => item.id === state.selectedSoftwareUserId)) {
      state.selectedSoftwareUserId = "";
    }
    if (!state.creatingSoftwareUser && !state.selectedSoftwareUserId && state.softwareUsers.length) {
      state.selectedSoftwareUserId = state.softwareUsers[0].id;
    }
    $("#software-user-count").textContent = `${state.softwareUsers.length} 个`;
    root.innerHTML = state.softwareUsers.length ? state.softwareUsers.map((user) => `<button type="button" class="software-user-item ${user.id === state.selectedSoftwareUserId ? "is-active" : ""} ${user.active ? "" : "is-disabled"}" data-software-user-id="${escapeHtml(user.id)}"><span class="software-user-avatar">${escapeHtml((user.display_name || user.username || "U").slice(0, 1).toUpperCase())}</span><span><b>${escapeHtml(user.display_name || user.username)}</b><small>${escapeHtml(user.username)} · ${user.active ? "启用" : "停用"}</small></span><em data-role="${escapeHtml(normalizeSoftwareRole(user.role))}">${escapeHtml(softwareRoleLabel(user.role))}</em></button>`).join("") : '<div class="empty-state compact-empty"><h3>还没有成员账号</h3><p>先创建一个启用的管理员账号。</p></div>';
    renderSoftwareUserEditor();
  }

  async function selectSoftwareUser(userId) {
    state.creatingSoftwareUser = false;
    state.selectedSoftwareUserId = String(userId || "");
    renderSoftwareUsers();
  }

  function resetSoftwareUserEditor() {
    state.creatingSoftwareUser = true;
    state.selectedSoftwareUserId = "";
    renderSoftwareUsers();
    $("#software-user-form [name='username']")?.focus();
  }

  async function saveSoftwareUser(form, button) {
    if (selectedSoftwareUser()?.role === "supervisor") {
      throw new Error("请先升级并重启主电脑，再编辑这个旧版主管账号。");
    }
    await withBusyButton(button, "正在保存…", async () => {
      const current = selectedSoftwareUser();
      const payload = {
        id: form.elements.id.value || undefined,
        username: form.elements.username.value.trim(),
        display_name: form.elements.display_name.value.trim(),
        role: form.elements.role.value,
        active: form.elements.active.checked,
      };
      const initialPassword = String(form.elements.initial_password.value || "");
      if (initialPassword) payload.initial_password = initialPassword;
      if (current?.row_version) payload.expected_version = Number(current.row_version);
      const saved = await checkedCall("save_software_user", payload);
      for (const select of $$('[data-user-permission]', form)) {
        const permission = select.dataset.userPermission;
        const desired = select.value === "inherit" ? null : select.value === "allow";
        const before = Object.prototype.hasOwnProperty.call(current?.permission_overrides || {}, permission)
          ? Boolean(current.permission_overrides[permission])
          : null;
        if (desired !== before) await checkedCall("set_user_permission", saved.id, permission, desired);
      }
      const list = await checkedCall("list_software_users");
      state.softwareUsers = Array.isArray(list?.items) ? list.items : [];
      state.creatingSoftwareUser = false;
      state.selectedSoftwareUserId = saved.id;
      form.elements.initial_password.value = "";
      renderSoftwareUsers();
      toast(`软件账号“${saved.display_name || saved.username}”已保存。`, "info");
    });
  }

  async function deleteSelectedSoftwareUser(button) {
    const user = selectedSoftwareUser();
    if (!user) return;
    const name = user.display_name || user.username;
    if (!window.confirm(`确定删除成员账号“${name}”？该账号会立即无法登录，已有制作记录会继续保留。`)) return;
    await withBusyButton(button, "正在删除…", async () => {
      await checkedCall("delete_software_user", user.id);
      const list = await checkedCall("list_software_users");
      state.softwareUsers = Array.isArray(list?.items) ? list.items : [];
      state.selectedSoftwareUserId = "";
      state.creatingSoftwareUser = false;
      renderSoftwareUsers();
      toast(`成员账号“${name}”已删除。`, "info");
    });
  }

  function managedDeviceById(deviceId) {
    return state.managedDevices.find((item) => item.id === String(deviceId || "")) || null;
  }

  function managedDeviceMemberLabel(userId) {
    const user = state.softwareUsers.find((item) => item.id === userId);
    return user ? (user.display_name || user.username) : (userId ? "已登记成员" : "未绑定成员");
  }

  function managedDeviceConfigState(device) {
    const revisionNumber = Number(device?.desired_revision_number || 0);
    if (!revisionNumber) return { label: "使用本机设置", className: "", detail: "尚未下发团队配置" };
    const revision = state.managedDeviceConfigs.find((item) => Number(item.revision_number) === revisionNumber) || null;
    const configDetail = revision ? state.managedDeviceConfigDetails.get(revision.id) : null;
    const target = configDetail?.targets?.find((item) => item.device_id === device.id) || null;
    const ack = String(target?.ack_status || "").toLowerCase();
    if (ack === "applied") return { label: `配置 r${revisionNumber} 已应用`, className: "is-applied", detail: target.acknowledged_at ? formatDeviceCreatedAt(target.acknowledged_at) : "已确认" };
    if (ack === "failed") return { label: `配置 r${revisionNumber} 应用失败`, className: "is-failed", detail: target.ack_message || "请在该电脑立即同步" };
    return { label: `配置 r${revisionNumber} 待应用`, className: "is-pending", detail: device.online ? "等待本机下一次同步" : "电脑联网后自动应用" };
  }

  function renderManagedConfigHistory() {
    const root = $("#managed-config-history");
    if (!root) return;
    const revisions = state.managedDeviceConfigs.slice(0, 5);
    if (!revisions.length) {
      root.innerHTML = '<span class="managed-config-history-empty">还没有下发记录</span>';
      return;
    }
    root.innerHTML = revisions.map((revision) => {
      const detail = state.managedDeviceConfigDetails.get(revision.id);
      const targets = Array.isArray(detail?.targets) ? detail.targets : [];
      const applied = targets.filter((item) => item.ack_status === "applied").length;
      const failed = targets.filter((item) => item.ack_status === "failed").length;
      const total = Number(revision.target_count || targets.length || 0);
      const pending = Math.max(0, total - applied - failed);
      const status = failed ? `${failed} 台失败` : pending ? `${pending} 台待应用` : total ? "全部已应用" : "等待设备";
      return `<article class="managed-config-history-row">
        <b>r${Number(revision.revision_number || 0)}</b>
        <span><b>${escapeHtml(revision.note || "团队制作默认值")}</b><small>${escapeHtml(formatDeviceCreatedAt(revision.created_at))} · ${total} 台</small></span>
        <em>${escapeHtml(status)}</em>
      </article>`;
    }).join("");
  }

  function renderManagedDeviceWorkspace() {
    const root = $("#managed-device-list");
    if (!root) return;
    const access = hubManagementAccessState();
    const devices = state.managedDevices;
    const activeDevices = devices.filter((item) => item.active !== false);
    const onlineCount = activeDevices.filter((item) => item.online).length;
    const selectedIds = new Set([...state.managedDeviceSelection].filter((deviceId) => activeDevices.some((item) => item.id === deviceId)));
    state.managedDeviceSelection = selectedIds;
    if ($("#managed-device-total")) $("#managed-device-total").textContent = String(devices.length);
    if ($("#managed-device-online")) $("#managed-device-online").textContent = String(onlineCount);
    if ($("#managed-device-disabled")) $("#managed-device-disabled").textContent = String(devices.length - activeDevices.length);
    if ($("#managed-device-selection")) $("#managed-device-selection").textContent = selectedIds.size ? `已选择 ${selectedIds.size} 台` : "尚未选择";
    if ($("#managed-target-selected-copy")) $("#managed-target-selected-copy").textContent = selectedIds.size ? `将发送到 ${selectedIds.size} 台电脑` : "请先选择设备";
    const refreshProof = $("#managed-device-refresh-proof");
    if (refreshProof) {
      refreshProof.textContent = state.managedDevicesLoading
        ? "正在读取主电脑设备…"
        : state.managedDevicesError
          ? `读取失败：${state.managedDevicesError}`
          : state.managedDevicesRefreshedAt
            ? `最近刷新 ${formatDeviceCreatedAt(state.managedDevicesRefreshedAt)} · 在线按 2 分钟心跳判断`
            : "等待读取主电脑设备";
    }
    const selectAll = $("#select-all-managed-devices");
    if (selectAll) {
      selectAll.disabled = !access.canManage || !activeDevices.length;
      selectAll.textContent = activeDevices.length && selectedIds.size === activeDevices.length ? "取消全选" : "全选可用设备";
    }
    const targetMode = $('input[name="managed-config-target"]:checked')?.value || "selected";
    const pushButton = $("#push-managed-device-config");
    if (pushButton) pushButton.disabled = !access.canManage || !activeDevices.length || (targetMode === "selected" && !selectedIds.size);

    if (!access.canManage) {
      const heading = access.restartRequired ? "重启主电脑后管理设备" : "主电脑服务尚未运行";
      root.innerHTML = `<div class="hub-device-empty is-warning"><span aria-hidden="true">!</span><b>${heading}</b><small>保存“设为主电脑”并重启 StoryForge 后，已登记设备和远程设置会出现在这里。</small></div>`;
      renderManagedConfigHistory();
      return;
    }
    if (state.managedDevicesLoading && !devices.length) {
      root.innerHTML = '<div class="hub-device-loading"><i></i><i></i><i></i></div>';
      renderManagedConfigHistory();
      return;
    }
    if (state.managedDevicesError && !devices.length) {
      root.innerHTML = `<div class="hub-device-empty is-error"><span aria-hidden="true">!</span><b>设备列表读取失败</b><small>${escapeHtml(state.managedDevicesError)}</small><button type="button" class="button button-ghost" data-refresh-managed-devices>重新读取</button></div>`;
      renderManagedConfigHistory();
      return;
    }
    if (!devices.length) {
      root.innerHTML = '<div class="hub-device-empty"><span aria-hidden="true">PC</span><b>还没有登记的制作电脑</b><small>让员工在自己的电脑输入主电脑地址、账号、密码和电脑名，连接成功后会自动出现在这里。</small></div>';
      renderManagedConfigHistory();
      return;
    }

    root.innerHTML = devices.map((device) => {
      const selected = selectedIds.has(device.id);
      const enabled = device.active !== false;
      const config = managedDeviceConfigState(device);
      const stateLabel = enabled ? (device.online ? "在线" : "离线") : "已停用";
      const systemLabel = [device.hostname, device.os_name, device.architecture].filter(Boolean).join(" · ") || "系统信息待上报";
      return `<article class="managed-device-row ${device.online && enabled ? "is-online" : ""} ${enabled ? "" : "is-disabled"} ${selected ? "is-selected" : ""}" data-managed-device-row="${escapeHtml(device.id)}">
        <label class="managed-device-check" title="${enabled ? "选择这台电脑" : "停用设备不能接收设置"}"><input type="checkbox" data-managed-device-select="${escapeHtml(device.id)}" ${selected ? "checked" : ""} ${enabled ? "" : "disabled"} aria-label="选择 ${escapeHtml(device.name || device.hostname || "制作电脑")}" /></label>
        <span class="managed-device-state-rail" aria-hidden="true"></span>
        <div class="managed-device-identity">
          <div class="managed-device-name-line"><b>${escapeHtml(device.name || device.hostname || "未命名电脑")}</b><em class="managed-device-member">${escapeHtml(managedDeviceMemberLabel(device.last_user_id))}</em>${device.needs_admin_review ? '<em class="managed-device-new-login">新电脑首次登录</em>' : ""}</div>
          <div class="managed-device-meta"><span>${escapeHtml(stateLabel)}</span><span>${escapeHtml(systemLabel)}</span><span>StoryForge ${escapeHtml(device.app_version || "版本未知")}</span><span>最后在线 ${escapeHtml(formatDeviceCreatedAt(device.last_seen_at))}</span></div>
        </div>
        <div class="managed-device-config-state">
          <span class="${config.className}">${escapeHtml(config.label)}</span>
          <small>${escapeHtml(config.detail)}</small>
          <div class="managed-device-actions">${device.needs_admin_review ? `<button type="button" class="button button-ghost" data-managed-device-action="review" data-managed-device-id="${escapeHtml(device.id)}">我已知道</button>` : ""}<button type="button" class="button button-ghost" data-managed-device-action="rename" data-managed-device-id="${escapeHtml(device.id)}">改名</button><button type="button" class="button button-ghost ${enabled ? "is-danger" : ""}" data-managed-device-action="toggle" data-managed-device-id="${escapeHtml(device.id)}">${enabled ? "停用" : "重新启用"}</button></div>
        </div>
      </article>`;
    }).join("");
    renderManagedConfigHistory();
  }

  async function loadManagedDeviceFleet({ silent = false } = {}) {
    if (!hubManagementAccessState().canManage) {
      state.managedDevicesLoading = false;
      state.managedDevicesError = "";
      renderManagedDeviceWorkspace();
      return;
    }
    const requestId = state.managedDevicesRequestId + 1;
    state.managedDevicesRequestId = requestId;
    state.managedDevicesError = "";
    if (!silent || !state.managedDevices.length) state.managedDevicesLoading = true;
    renderManagedDeviceWorkspace();
    try {
      const [deviceData, configData] = await Promise.all([
        checkedCall("list_managed_devices", { limit: 500, offset: 0 }),
        checkedCall("list_managed_device_configs", 25, 0),
      ]);
      if (requestId !== state.managedDevicesRequestId) return;
      const devices = Array.isArray(deviceData?.items) ? deviceData.items : [];
      const revisions = Array.isArray(configData?.items) ? configData.items : [];
      const nextDetails = new Map(state.managedDeviceConfigDetails);
      const details = await Promise.all(revisions.slice(0, 10).map(async (revision) => {
        const cached = nextDetails.get(revision.id);
        const hasPendingTarget = cached?.targets?.some((item) => !item.ack_status);
        if (cached && !hasPendingTarget) return cached;
        try {
          return await checkedCall("get_managed_device_config", revision.id);
        } catch (_error) {
          return cached || null;
        }
      }));
      if (requestId !== state.managedDevicesRequestId) return;
      details.filter(Boolean).forEach((detail) => nextDetails.set(detail.id, detail));
      state.managedDevices = devices;
      state.managedDeviceConfigs = revisions;
      state.managedDeviceConfigDetails = nextDetails;
      state.managedDevicesRefreshedAt = new Date().toISOString();
    } catch (error) {
      if (requestId !== state.managedDevicesRequestId) return;
      state.managedDevicesError = error.message || "无法读取制作电脑。";
      if (!silent) toast(state.managedDevicesError, "error");
    } finally {
      if (requestId === state.managedDevicesRequestId) {
        state.managedDevicesLoading = false;
        renderManagedDeviceWorkspace();
      }
    }
  }

  function syncManagedConfigControlsFromSettings() {
    const settings = state.settings || {};
    assignValue("#managed-config-wpm", settings.narration_wpm ?? 240);
    assignValue("#managed-config-bgm", Math.round(Number(settings.bgm_volume ?? 0.28) * 100));
    if ($("#managed-config-bgm-value")) $("#managed-config-bgm-value").textContent = `${Math.round(Number(settings.bgm_volume ?? 0.28) * 100)}%`;
    assignValue("#managed-config-fps", settings.output_fps || 60);
    assignValue("#managed-config-language", settings.language || "en-US");
  }

  function portableManagedDeviceConfigPayload() {
    const settings = state.settings || {};
    const {
      outro_card: _legacyOutroCard,
      outro_card_preset: _legacyOutroPreset,
      ...portableStyle
    } = stylePayload();
    return {
      ...portableStyle,
      language: $("#managed-config-language")?.value || settings.language || "en-US",
      narration_wpm: Number($("#managed-config-wpm")?.value || settings.narration_wpm || 240),
      bgm_volume: Number($("#managed-config-bgm")?.value ?? Math.round(Number(settings.bgm_volume ?? 0.28) * 100)) / 100,
      output_fps: Number($("#managed-config-fps")?.value || settings.output_fps || 60),
      output_width: Number(settings.output_width || 1080),
      output_height: Number(settings.output_height || 1920),
      max_episode_minutes: Number(settings.max_episode_minutes || 10),
      end_card_seconds: Number(settings.end_card_seconds || 6),
    };
  }

  function setManagedConfigStatus(message, stateName = "idle") {
    const status = $("#managed-config-status");
    if (!status) return;
    status.textContent = message;
    status.dataset.state = stateName;
  }

  async function pushManagedDeviceConfig(button) {
    const activeIds = new Set(state.managedDevices.filter((item) => item.active !== false).map((item) => item.id));
    const selectedIds = [...state.managedDeviceSelection].filter((deviceId) => activeIds.has(deviceId));
    const sendAll = ($('input[name="managed-config-target"]:checked')?.value || "selected") === "all";
    if (!sendAll && !selectedIds.length) throw new Error("请先选择至少一台可用电脑。 ");
    const targetMode = sendAll ? "all" : selectedIds.length === 1 ? "single" : "multiple";
    const payload = {
      target_mode: targetMode,
      device_ids: sendAll ? [] : selectedIds,
      config: portableManagedDeviceConfigPayload(),
      note: $("#managed-config-note")?.value.trim() || "",
    };
    setManagedConfigStatus("正在建立新的配置版本…", "working");
    await withBusyButton(button, "正在下发…", async () => {
      const revision = await checkedCall("create_managed_device_config", payload);
      if ($("#managed-config-note")) $("#managed-config-note").value = "";
      setManagedConfigStatus(`配置 r${Number(revision.revision_number || 0)} 已下发，电脑联网后会自动应用。`, "ready");
      await loadManagedDeviceFleet({ silent: true });
      toast(`制作设置已发送到 ${sendAll ? "全部可用电脑" : `${selectedIds.length} 台电脑`}。`, "info");
    });
  }

  async function openManagedDeviceDialog(deviceId) {
    const current = managedDeviceById(deviceId);
    if (!current) throw new Error("设备列表已经变化，请刷新后重试。 ");
    const latest = await checkedCall("get_managed_device", deviceId);
    const dialog = $("#managed-device-dialog");
    const form = $("#managed-device-form");
    form.elements.device_id.value = latest.id;
    form.elements.device_name.value = latest.name || current.name || "";
    $("#managed-device-dialog-title").textContent = `修改“${latest.name || current.name || "制作电脑"}”`;
    $("#managed-device-dialog-status").textContent = "保存后，名称会在该电脑下一次同步时更新。";
    if (dialog?.showModal) dialog.showModal();
    else dialog?.setAttribute("open", "");
    window.setTimeout(() => form.elements.device_name.focus(), 0);
  }

  function closeManagedDeviceDialog() {
    const dialog = $("#managed-device-dialog");
    if (dialog?.open) dialog.close();
    $("#managed-device-form")?.reset();
  }

  async function saveManagedDeviceName(form, button) {
    const deviceId = form.elements.device_id.value;
    const name = form.elements.device_name.value.trim();
    if (!deviceId || !name) throw new Error("电脑名称不能为空。 ");
    await withBusyButton(button, "正在保存…", async () => {
      await checkedCall("rename_managed_device", deviceId, name);
      closeManagedDeviceDialog();
      await loadManagedDeviceFleet({ silent: true });
      toast(`电脑名称已改为“${name}”。`, "info");
    });
  }

  async function toggleManagedDevice(deviceId, button) {
    const device = managedDeviceById(deviceId);
    if (!device) throw new Error("设备列表已经变化，请刷新后重试。 ");
    const nextActive = device.active === false;
    if (!nextActive && !window.confirm(`停用“${device.name}”吗？这台电脑会立即断开，需要再次使用账号密码连接才能恢复。`)) return;
    await withBusyButton(button, nextActive ? "正在启用…" : "正在停用…", async () => {
      await checkedCall("set_managed_device_active", device.id, nextActive, true);
      state.managedDeviceSelection.delete(device.id);
      await loadManagedDeviceFleet({ silent: true });
      toast(nextActive ? `“${device.name}”已重新启用。` : `“${device.name}”已停用并撤销原连接。`, "info");
    });
  }

  function renderDeviceSyncStatus() {
    const status = state.deviceSyncStatus || state.hubRuntimeStatus?.device_sync || {};
    const stateName = String(status.state || "idle");
    const enabled = status.enabled !== false && Boolean(status.device_id || state.settings?.hub?.device_id);
    const labels = {
      ready: ["制作设置已同步", "已同步"],
      syncing: ["正在同步制作设置", "同步中"],
      offline: ["暂时无法连接主电脑", "离线"],
      legacy_token: ["请用账号密码重新登录", "需重连"],
      idle: [enabled ? "等待首次同步" : "尚未登记这台电脑", enabled ? "等待" : "未启用"],
    };
    const [title, badge] = labels[stateName] || labels.idle;
    if ($("#device-sync-title")) $("#device-sync-title").textContent = title;
    if ($("#device-sync-badge")) $("#device-sync-badge").textContent = badge;
    const pulse = $("#device-sync-pulse");
    if (pulse) pulse.className = `device-sync-pulse ${stateName === "ready" ? "is-ready" : stateName === "syncing" ? "is-working" : ["offline", "legacy_token"].includes(stateName) ? "is-error" : ""}`;
    const copy = $("#device-sync-copy");
    if (copy) copy.textContent = stateName === "ready"
      ? `这台电脑会每 ${Number(status.poll_seconds || 20)} 秒检查一次新设置；渲染任务和素材仍在本机处理。`
      : stateName === "legacy_token"
        ? "这台电脑使用的连接方式已过期。请在上方使用账号和密码重新登录一次。"
        : stateName === "offline"
          ? "本机仍可继续工作；恢复主电脑连接后会自动补齐最新设置。"
          : "连接主电脑后，这台电脑会自动接收管理员下发的字幕、语速和输出设置。";
    const revisionId = String(status.applied_revision_id || "");
    if ($("#device-sync-revision")) $("#device-sync-revision").textContent = revisionId ? `…${revisionId.slice(-8)}` : "本机默认";
    if ($("#device-sync-time")) $("#device-sync-time").textContent = status.last_success_at ? formatDeviceCreatedAt(status.last_success_at) : "尚未同步";
    const error = $("#device-sync-error");
    if (error) {
      error.textContent = status.last_error || "";
      error.classList.toggle("is-hidden", !status.last_error);
    }
    const button = $("#sync-device-config-now");
    if (button) button.disabled = !enabled || stateName === "syncing";
  }

  async function loadDeviceSyncStatus({ refreshSettings = false, silent = false } = {}) {
    const runtimeMode = String(state.hubRuntimeStatus?.runtime_mode || state.hubRuntimeStatus?.mode || state.settings?.hub?.mode || "local");
    if (runtimeMode !== "client" || isAuthenticatedHubBrowser()) {
      renderDeviceSyncStatus();
      return;
    }
    const previousRevision = String(state.deviceSyncStatus?.applied_revision_id || "");
    try {
      const status = await checkedCall("get_device_sync_status");
      state.deviceSyncStatus = status;
      const revisionChanged = Boolean(status.applied_revision_id && status.applied_revision_id !== previousRevision);
      if (refreshSettings || revisionChanged) {
        const fresh = await checkedCall("get_bootstrap");
        if (fresh?.settings) state.settings = fresh.settings;
        if (fresh?.hub_status) state.hubRuntimeStatus = fresh.hub_status;
        loadSettingsIntoControls();
      }
    } catch (error) {
      state.deviceSyncStatus = { ...(state.deviceSyncStatus || {}), state: "offline", last_error: error.message || "同步状态读取失败。" };
      if (!silent) toast(state.deviceSyncStatus.last_error, "error");
    }
    renderDeviceSyncStatus();
  }

  async function syncDeviceConfigNow(button) {
    await withBusyButton(button, "正在同步…", async () => {
      state.deviceSyncStatus = { ...(state.deviceSyncStatus || {}), state: "syncing", last_error: "" };
      renderDeviceSyncStatus();
      const status = await checkedCall("sync_device_config_now");
      state.deviceSyncStatus = status;
      await loadDeviceSyncStatus({ refreshSettings: true, silent: true });
      toast("这台电脑的制作设置已同步。", "info");
    });
  }

  async function refreshHubDeviceWorkspace({ silent = false } = {}) {
    const runtimeMode = String(state.hubRuntimeStatus?.runtime_mode || state.hubRuntimeStatus?.mode || state.settings?.hub?.mode || "local");
    if (runtimeMode === "host") await loadManagedDeviceFleet({ silent });
    else if (runtimeMode === "client") await loadDeviceSyncStatus({ silent });
    else {
      renderManagedDeviceWorkspace();
      renderDeviceSyncStatus();
    }
  }

  function stopManagedDeviceFleetPolling() {
    if (state.managedDeviceFleetTimer) window.clearInterval(state.managedDeviceFleetTimer);
    state.managedDeviceFleetTimer = null;
  }

  function startManagedDeviceFleetPolling() {
    stopManagedDeviceFleetPolling();
    state.managedDeviceFleetTimer = window.setInterval(() => {
      if (document.hidden || !$('[data-view="hub"]')?.classList.contains("is-visible")) return;
      void refreshHubDeviceWorkspace({ silent: true });
    }, 20_000);
  }

  async function loadProductionRecordGroups({ silent = false, expectedPollEpoch = null } = {}) {
    const filters = {
      status: state.recordStatusFilter,
      novel_id: state.recordNovelFilter,
      batch_id: state.recordBatchFilter,
      created_by_user_id: state.recordMemberFilter,
      device_id: state.recordDeviceFilter,
      created_from: state.recordDateFrom ? `${state.recordDateFrom}T00:00:00` : "",
      created_to: state.recordDateTo ? `${state.recordDateTo}T23:59:59.999999` : "",
      archived: false,
      trashed: Boolean(state.recordTrashFilter),
      limit: 5000,
    };
    try {
      const groups = await checkedCall("get_production_record_groups", filters);
      if (expectedPollEpoch !== null && expectedPollEpoch !== state.pollEpoch) return false;
      state.productionRecordGroups = groups;
    } catch (error) {
      if (expectedPollEpoch !== null && expectedPollEpoch !== state.pollEpoch) return false;
      state.productionRecordGroups = null;
      if (!silent && !isBrowserDemo) toast(error.message, "error");
    }
    renderRecords();
    return true;
  }

  function productionRecordsViewVisible() {
    return Boolean($('[data-view="records"]')?.classList.contains("is-visible"));
  }

  async function runScheduledProductionRecordRefresh() {
    state.recordPollTimer = null;
    if (!state.recordPollPending || !productionRecordsViewVisible()) {
      state.recordPollPending = false;
      state.recordPollUrgent = false;
      return;
    }
    if (state.recordPollInFlight) return;
    const pollEpoch = state.pollEpoch;
    state.recordPollPending = false;
    state.recordPollUrgent = false;
    state.recordPollInFlight = true;
    state.recordPollLastAt = Date.now();
    try {
      await loadProductionRecordGroups({ silent: true, expectedPollEpoch: pollEpoch });
    } finally {
      state.recordPollInFlight = false;
      if (state.recordPollPending && productionRecordsViewVisible()) {
        scheduleProductionRecordRefresh({ urgent: state.recordPollUrgent });
      }
    }
  }

  function scheduleProductionRecordRefresh({ urgent = false } = {}) {
    if (!productionRecordsViewVisible()) return;
    state.recordPollPending = true;
    state.recordPollUrgent = state.recordPollUrgent || urgent;
    if (state.recordPollInFlight) return;
    const waitMs = state.recordPollUrgent
      ? 0
      : Math.max(0, RECORD_POLL_INTERVAL_MS - (Date.now() - state.recordPollLastAt));
    if (state.recordPollTimer) {
      if (!state.recordPollUrgent) return;
      window.clearTimeout(state.recordPollTimer);
    }
    state.recordPollTimer = window.setTimeout(() => {
      void runScheduledProductionRecordRefresh();
    }, waitMs);
  }

  function selectedProductionRecordTasks() {
    return groupedProductionTasks().filter((task) =>
      state.selectedRecordIds.has(String(task.id)),
    );
  }

  async function runSelectedRecordAction(action, button) {
    const selected = selectedProductionRecordTasks();
    if (!selected.length) return;
    await withBusyButton(button, "处理中…", async () => {
      if (action === "retry") {
        const failures = [];
        for (const task of selected) {
          if (!["failed", "cancelled", "interrupted", "skipped"].includes(task.raw_status || task.status)) continue;
          if (!task.job_id) {
            failures.push(`${task.episode_label || task.title}: 缺少任务编号`);
            continue;
          }
          try {
            await checkedCall("retry_failed", task.job_id);
          } catch (error) {
            failures.push(`${task.episode_label || task.title}: ${error.message}`);
          }
        }
        if (failures.length) toast(`部分任务未能重试：${failures.slice(0, 3).join("；")}`, "error");
      } else if (action === "cancel") {
        await checkedCall(
          "cancel_production_records",
          selected.map((task) => task.id),
          "操作员在生产记录页立即取消",
        );
      } else if (action === "trash") {
        await checkedCall("trash_production_records", selected.map((task) => task.id));
      } else if (action === "restore") {
        await checkedCall("restore_trashed_production_records", selected.map((task) => task.id));
      } else if (action === "delete") {
        if (!window.confirm(`彻底删除所选 ${selected.length} 条 Hub 记录？员工电脑里的视频不会被删除。`)) return;
        await checkedCall("delete_trashed_production_records", selected.map((task) => task.id));
      }
      state.selectedRecordIds.clear();
      await loadProductionRecordGroups();
      toast(action === "delete" ? "记录已删除；员工电脑文件未改动。" : "生产记录已更新。", "info");
    });
  }

  async function loadLibraryBootstrap() {
    try {
      const data = await checkedCall("get_library_bootstrap");
      state.novels = Array.isArray(data?.novels) ? data.novels : [];
      state.publishingAccounts = Array.isArray(data?.publishing_accounts) ? data.publishing_accounts : [];
      state.productionRecords = Array.isArray(data?.production_records) ? data.production_records : [];
      state.softwareUsers = Array.isArray(data?.users) ? data.users : [];
      const accountsEntry = $('[data-open-view="accounts"]');
      if (accountsEntry) {
        accountsEntry.classList.toggle(
          "is-hidden",
          state.settings?.hub?.mode === "client" && !state.softwareUsers.length,
        );
      }
      state.libraryBackendReady = true;
      state.libraryBackendError = "";
      await loadProductionRecordGroups({ silent: true });
    } catch (error) {
      state.libraryBackendReady = false;
      state.libraryBackendError = String(error?.message || "小说库暂时无法读取，请检查主电脑连接后重试。");
      if (!isBrowserDemo) toast(error.message, "error");
    }
    renderNovelLibrary();
    renderRecords();
    renderSoftwareUsers();
    renderPublishingAccounts();
    renderProductionWorkbench();
  }

  function resetNovelImport(targetNovel = null) {
    const form = $("#novel-import-form");
    form?.reset();
    syncImportLanguageOptions();
    state.importSource = "paste";
    state.importFilePath = "";
    state.importTargetNovelId = targetNovel?.id || "";
    $$('[data-import-source]').forEach((button) => {
      const active = button.dataset.importSource === "paste";
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    $("#import-paste-field")?.classList.remove("is-hidden");
    $("#import-file-proof")?.classList.add("is-hidden");
    if (targetNovel) {
      $("#import-title").value = targetNovel.title || "";
      $("#import-synopsis").value = targetNovel.synopsis || "";
      const languageInfo = novelLanguageInfo(targetNovel);
      $("#import-language").value = languageInfo.manual ? languageEditorCode(languageInfo.code) : "";
    }
    if ($("#novel-import-title")) $("#novel-import-title").textContent = targetNovel ? "更新小说正文版本" : "添加一部小说";
    if ($("#submit-novel-import")) $("#submit-novel-import").textContent = targetNovel ? "导入为新版本" : "导入小说";
    if ($("#import-dialog-status")) $("#import-dialog-status").textContent = targetNovel ? "将正文更新到当前小说，不会新建重复书。" : "当前来源：粘贴正文";
  }

  function openNovelImport(novel = null) {
    const targetNovel = novel?.id ? novel : null;
    resetNovelImport(targetNovel);
    const dialog = $("#novel-import-dialog");
    if (dialog?.showModal) dialog.showModal();
    else dialog?.setAttribute("open", "");
    window.setTimeout(() => $("#import-text-content")?.focus(), 0);
  }

  function closeNovelImport() {
    const dialog = $("#novel-import-dialog");
    if (dialog?.open && dialog.close) dialog.close();
    else dialog?.removeAttribute("open");
  }

  async function chooseNovelImportSource(source) {
    state.importSource = source;
    $$('[data-import-source]').forEach((button) => {
      const active = button.dataset.importSource === source;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const paste = source === "paste";
    $("#import-paste-field").classList.toggle("is-hidden", !paste);
    $("#import-file-proof").classList.toggle("is-hidden", paste);
    if (paste) {
      state.importFilePath = "";
      $("#import-dialog-status").textContent = "当前来源：粘贴正文";
      $("#import-text-content")?.focus();
      return;
    }
    try {
      $("#import-dialog-status").textContent = `正在选择 ${source.toUpperCase()}…`;
      const path = await checkedCall("choose_file", source);
      state.importFilePath = String(path || "");
      $("#import-file-kind").textContent = source.toUpperCase();
      $("#import-file-name").textContent = webAssetDisplayName(path);
      $("#import-file-path").textContent = path
        ? isWebRuntime && !isBrowserDemo ? "已安全上传到 StoryForge Hub，本次会话可用。" : path
        : "尚未选择文件";
      $("#import-dialog-status").textContent = path ? `已选择 ${source.toUpperCase()} 文件` : "未选择文件";
      if (path && !$("#import-title").value.trim()) {
        $("#import-title").value = webAssetDisplayName(path).replace(/\.(txt|docx)$/i, "");
      }
    } catch (error) {
      state.importFilePath = "";
      $("#import-dialog-status").textContent = error.message;
      toast(error.message, "error");
    }
  }

  function novelFromApiResult(data) {
    return data?.novel || data;
  }

  async function submitNovelImport(form, button) {
    const title = $("#import-title").value.trim();
    const synopsis = $("#import-synopsis").value.trim();
    const language = $("#import-language").value;
    const payload = { title, synopsis };
    if (language) payload.language = language;
    if (state.importTargetNovelId) payload.novel_id = state.importTargetNovelId;
    const method = state.importSource === "paste" ? "import_novel_text" : "import_novel_file";
    if (state.importSource === "paste") payload.text = $("#import-text-content").value;
    else Object.assign(payload, { file_path: state.importFilePath, source_type: state.importSource });
    await withBusyButton(button, "正在导入…", async () => {
      const novel = novelFromApiResult(await checkedCall(method, payload));
      upsertNovel(novel);
      renderNovelLibrary();
      closeNovelImport();
      if (state.importTargetNovelId) {
        renderNovelDetail();
        toast(`“${novel.title}”正文已保存为新的内容版本。`);
      } else {
        toast(`“${novel.title}”已加入小说库。`);
        await openNovelDetail(novel.id, $("#open-novel-import"));
      }
    });
  }

  async function saveNovelMetadata(button) {
    const novel = state.selectedNovel;
    if (!novel) return;
    const title = $("#detail-edit-title")?.value.trim();
    const synopsis = $("#detail-edit-synopsis")?.value.trim();
    const languageOverride = $("#detail-language-override")?.value || "";
    if (!title) {
      toast("小说标题不能为空。", "error");
      return;
    }
    try {
      await withBusyButton(button, "正在保存…", async () => {
        const payload = { id: novel.id, title, synopsis };
        if (languageOverride) payload.language = languageOverride;
        else if (novelLanguageInfo(novel).manual) payload.redetect_language = true;
        const updated = novelFromApiResult(await checkedCall("save_novel", payload));
        upsertNovel(updated);
        renderNovelLibrary();
        renderNovelDetail();
        toast(languageOverride ? "小说资料和语种分类已保存。" : "小说资料已保存。");
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function redetectNovelLanguage(button) {
    const novel = state.selectedNovel;
    if (!novel) return;
    try {
      await withBusyButton(button, "正在识别…", async () => {
        const updated = novelFromApiResult(await checkedCall("save_novel", { id: novel.id, redetect_language: true }));
        upsertNovel(updated);
        renderNovelLibrary();
        renderNovelDetail();
        const result = novelLanguageInfo(updated);
        toast(`已重新识别为${result.label}${result.confidence === null ? "" : `（${Math.round(result.confidence * 100)}%）`}。`, result.lowConfidence ? "info" : "info");
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function chooseNovelCover(button) {
    const novel = state.selectedNovel;
    if (!novel) return;
    try {
      await withBusyButton(button, "正在选择…", async () => {
        const coverPath = await checkedCall("choose_file", "cover");
        if (!coverPath) return;
        const updated = novelFromApiResult(await checkedCall("save_novel", { id: novel.id, cover_path: coverPath }));
        upsertNovel(updated);
        renderNovelLibrary();
        renderNovelDetail();
        toast("小说封面已保存；封面结尾会使用舒适的轻推动画铺满全屏。", "info");
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function importNovelSynopsis(button) {
    try {
      await withBusyButton(button, "正在读取…", async () => {
        const filePath = await checkedCall("choose_file", "summary");
        if (!filePath) return;
        const data = await checkedCall("read_text_document", filePath);
        const textarea = $("#detail-edit-synopsis");
        if (textarea) textarea.value = data?.text || "";
        const proof = $("#synopsis-import-proof");
        if (proof) proof.textContent = `已读取 ${pathLeaf(data?.file_path || filePath)}；点击“保存资料”后写入小说库。`;
        toast("故事简介已填入，请确认内容后保存资料。", "info");
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function saveNovelBinding(button) {
    const novel = state.selectedNovel;
    const platformId = $("#detail-platform-select")?.value || "";
    if (!novel) return;
    try {
      await withBusyButton(button, "正在绑定…", async () => {
        const updated = novelFromApiResult(await checkedCall("save_novel_binding", { novel_id: novel.id, platform_id: platformId }));
        upsertNovel(updated);
        activeDraft(updated).platform_id = platformId;
        renderNovelLibrary();
        renderNovelDetail();
        toast(`已绑定 ${platformById(platformId)?.name || "平台"}。`);
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function addPromoCode(button) {
    const novel = state.selectedNovel;
    const platformId = activeDraft(novel).platform_id;
    const code = $("#new-promo-code")?.value.trim() || "";
    if (!novel) return;
    try {
      await withBusyButton(button, "正在添加…", async () => {
        const data = await checkedCall("add_promo_code", { novel_id: novel.id, platform_id: platformId, code });
        const updated = novelFromApiResult(data);
        upsertNovel(updated);
        if (data?.promo_code?.id) activeDraft(updated).promo_code_id = data.promo_code.id;
        renderNovelLibrary();
        renderNovelDetail();
        toast(`口令 ${data?.promo_code?.value || code.toUpperCase()} 已添加。`);
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function togglePromoCode(button) {
    const novel = state.selectedNovel;
    if (!novel) return;
    const platformId = activeDraft(novel).platform_id;
    try {
      const data = await checkedCall("update_promo_code", {
        novel_id: novel.id,
        platform_id: platformId,
        promo_code_id: button.dataset.toggleCode,
        active: button.dataset.nextActive === "true",
      });
      const updated = novelFromApiResult(data);
      upsertNovel(updated);
      renderNovelDetail();
      renderNovelLibrary();
      toast(`口令已${data?.promo_code?.active ? "启用" : "停用"}。`);
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function generateVoiceCandidates(button) {
    const novel = state.productionNovel || state.selectedNovel;
    if (!novel) return;
    const currentDraft = state.productionNovel?.id === novel.id
      ? structuredClone(activeDraft(novel))
      : null;
    const mood = $("#voice-candidate-mood")?.value || "suspense";
    try {
      await withBusyButton(button, "正在生成女声候选…", async () => {
        const data = await checkedCall("generate_voice_candidates", novel.id, mood);
        const updated = data?.novel || { ...novel, voice_candidates: data?.candidates || [] };
        if (!updated.voice_candidates?.length && data?.candidates) updated.voice_candidates = data.candidates;
        if (currentDraft) updated.draft = { ...(updated.draft || {}), ...currentDraft, story_mood: mood };
        upsertNovel(updated);
        if (state.productionNovel?.id === updated.id) renderProductionWorkbench();
        else renderNovelDetail();
        const candidates = updated.voice_candidates?.length ? updated.voice_candidates : (data?.candidates || []);
        const count = candidates.length;
        const actualProvider = candidates[0]?.provider;
        toast(`已使用 ${ttsProviderLabel(actualProvider)} 生成 ${count} 个${novelLanguageInfo(updated).label}女声候选，请试听后选择本批固定声线。`, "info");
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function generateIntroCardCopy(button) {
    const novel = state.productionNovel;
    if (!novel) return;
    syncProductionDraftFromControls();
    const draft = activeDraft(novel);
    try {
      await withBusyButton(button, "正在优化简介…", async () => {
        const result = await checkedCall(
          "generate_intro_card_copy",
          novel.id,
          [...(draft.episode_ids || [])],
        );
        draft.intro_card_text = String(result?.text || "").trim();
        draft.intro_card_source = String(result?.source || "preview_fallback");
        renderProductionWorkbench();
        toast(
          draft.intro_card_source.endsWith("_ai")
            ? "简介已由 AI 优化并冻结；预览和最终成片会使用同一份文字。"
            : "已使用小说简介生成安全文案；预览和最终成片会保持一致。",
          "info",
        );
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function playVoiceCandidate(button) {
    const novel = state.productionNovel || state.selectedNovel;
    const candidate = novel?.voice_candidates?.[Number(button.dataset.previewVoiceIndex)];
    if (!candidate) return;
    const audioUri = webAssetUrl(candidate.audio_uri || candidate.audio_path || "");
    if (audioUri && !String(candidate.audio_uri || "").startsWith("mock://")) {
      const audio = new Audio(audioUri);
      audio.play().catch(() => toast("候选音频暂时无法播放，请重新生成后重试。", "error"));
      return;
    }
    if (!window.speechSynthesis || typeof window.SpeechSynthesisUtterance !== "function") {
      toast("当前浏览器不支持语音试听；桌面版会播放实际候选音频。", "error");
      return;
    }
    window.speechSynthesis.cancel();
    const utterance = new window.SpeechSynthesisUtterance(candidate.excerpt || novel.synopsis || "Listen to this narration voice.");
    utterance.lang = ({ en: "en-US", ja: "ja-JP", zh: "zh-CN", es: "es-ES", fr: "fr-FR", hi: "hi-IN", it: "it-IT", pt: "pt-BR", id: "id-ID", de: "de-DE", ko: "ko-KR" })[novelLanguageInfo(novel).key] || "en-US";
    utterance.rate = candidate.profile === "confident" ? 1.02 : candidate.profile === "intimate" ? 0.9 : 0.96;
    utterance.pitch = candidate.profile === "warm" ? 1.08 : candidate.profile === "confident" ? 0.94 : 1.02;
    const voices = window.speechSynthesis.getVoices?.() || [];
    const languagePrefix = utterance.lang.split("-")[0];
    utterance.voice = (languagePrefix === "en"
      ? voices.find((voice) => /^en-US$/i.test(voice.lang) && /female|zira|samantha|ava|jenny|aria/i.test(voice.name))
      : null)
      || voices.find((voice) => String(voice.lang || "").toLocaleLowerCase().startsWith(languagePrefix))
      || null;
    window.speechSynthesis.speak(utterance);
    toast(`正在试听 ${candidate.label || candidate.voice_id}。`, "info");
  }


  async function chooseDraftFolder(button) {
    const novel = state.productionNovel || state.selectedNovel;
    if (!novel) return;
    const key = button.dataset.draftFolder;
    try {
      const value = await checkedCall("choose_folder", key);
      if (!value) return;
      if (state.localWorker) {
        state.localWorker.folders[key] = value;
        state.webDefaultFolders[key] = value;
      }
      activeDraft(novel)[key] = value;
      if (state.productionNovel?.id === novel.id) markProductionRecipeDirty();
      const input = $(`[data-draft-path="${key}"]`);
      if (input) input.value = value;
      if (state.productionNovel?.id === novel.id) syncProductionDraftFromControls();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function productionDraftPayload({ forQueue = false } = {}) {
    const novel = state.productionNovel;
    if (!novel) throw new Error("请先在制作台选择一部小说。");
    syncProductionDraftFromControls();
    const draft = activeDraft(novel);
    if (!draft.platform_id || !draft.promo_code_id) throw new Error("请选择已绑定的平台和本批口令。");
    if (!draft.episode_ids.length) throw new Error("至少选择一个分集。");
    if (draft.target_video_count < 1) throw new Error("生成总视频数至少为 1。");
    const outputMode = normalizedProductionOutputMode(draft.production_settings?.output_mode);
    const audioOnly = outputMode === "audio_only";
    const reuseAudio = outputMode === "reuse_audio";
    const bgmMode = String(draft.production_settings?.bgm_mode || "auto");
    if (forQueue && !reuseAudio && (!draft.voice?.provider || !draft.voice?.voice_id)) throw new Error("请试听并选择本批女声。");
    if (forQueue && reuseAudio && !String(draft.source_narration_audio || "").trim()) throw new Error("请选择已有配音。");
    if (forQueue && !draft.output_folder) throw new Error("开始生成前，请选择输出文件夹。");
    if (forQueue && !audioOnly && !draft.video_folder) throw new Error("开始完整生成前，请选择视频素材文件夹。");
    if (forQueue && !audioOnly && bgmMode === "auto" && !draft.music_folder) throw new Error("自动匹配背景音乐前，请选择背景音乐文件夹。");
    if (forQueue && !audioOnly && bgmMode === "manual" && !String(draft.production_settings?.bgm_file || "").trim()) throw new Error("手动指定背景音乐时，请选择音乐文件。");
    return {
      id: draft.id || "",
      row_version: Number(draft.row_version || 0) || undefined,
      novel_id: novel.id,
      platform_id: draft.platform_id,
      promo_code_id: draft.promo_code_id,
      publishing_account_id: draft.publishing_account_id,
      episode_ids: [...draft.episode_ids],
      target_video_count: audioOnly ? 1 : draft.target_video_count,
      story_mood: draft.story_mood || "suspense",
      story_mood_source: draft.story_mood_source || "auto",
      variant_count: audioOnly ? 1 : Math.max(1, draft.target_video_count),
      approvals: structuredClone(draft.approvals),
      voice: structuredClone(draft.voice || {}),
      voice_profile: draft.voice?.profile || "",
      production_settings: structuredClone(draft.production_settings || {}),
      subtitle_style_id: draft.production_settings?.subtitle_preset || "clear_outline",
      outro_style_id: draft.production_settings?.outro_card_preset || "editorial_white",
      applied_production_preset_id: String(draft.applied_production_preset_id || ""),
      applied_production_preset_revision: Number(draft.applied_production_preset_revision || 0),
      applied_production_preset_hash: String(draft.applied_production_preset_hash || ""),
      production_preset_dirty: Boolean(draft.production_preset_dirty),
      intro_card_text: String(draft.intro_card_text || storyPreviewText(novel, draft)),
      intro_card_source: String(
        draft.intro_card_source || (String(novel.synopsis || "").trim() ? "novel_synopsis" : "episode_excerpt"),
      ),
      video_folder: draft.video_folder,
      music_folder: draft.music_folder,
      output_folder: draft.output_folder,
      source_narration_audio: String(draft.source_narration_audio || ""),
    };
  }

  function applySavedDraftResult(data, fallbackNovel, payload) {
    const updated = data?.novel || { ...fallbackNovel, draft: data?.draft || payload };
    upsertNovel(updated);
    if (data?.draft && state.productionNovel?.id === fallbackNovel.id) {
      state.productionNovel.draft = data.draft;
    }
    if (data?.record) {
      const index = state.productionRecords.findIndex((item) => item.id === data.record.id);
      if (index >= 0) state.productionRecords[index] = data.record;
      else state.productionRecords.unshift(data.record);
    }
    return data?.draft || activeDraft(state.productionNovel || updated);
  }

  async function saveProductionDraft(button) {
    const novel = state.productionNovel;
    if (!novel) return;
    try {
      const payload = productionDraftPayload();
      await withBusyButton(button, "正在保存…", async () => {
        const data = await checkedCall("save_production_draft", payload);
        applySavedDraftResult(data, novel, payload);
        renderNovelLibrary();
        renderRecords();
        renderProductionWorkbench();
        toast("本批制作方案已保存。", "info");
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function queueProductionDraft(button) {
    const novel = state.productionNovel;
    if (!novel) return;
    try {
      const payload = productionDraftPayload({ forQueue: true });
      await withBusyButton(button, "正在创建完整任务…", async () => {
        const saved = await checkedCall("save_production_draft", payload);
        const savedDraft = applySavedDraftResult(saved, novel, payload);
        const queuePayload = {
          draft_id: savedDraft.id,
          novel_id: novel.id,
          preview_required: false,
          video_folder: payload.video_folder,
          music_folder: payload.music_folder,
          output_folder: payload.output_folder,
          source_narration_audio: payload.source_narration_audio,
        };
        const queued = await checkedCall("queue_production_draft", queuePayload);
        if (queued?.draft) applySavedDraftResult({ draft: queued.draft }, novel, payload);
        const queuedDraft = structuredClone(queued?.draft || savedDraft);
        const incoming = Array.isArray(queued?.jobs) ? queued.jobs : [];
        incoming.forEach((job) => {
          const index = state.jobs.findIndex((item) => item.id === job.id);
          if (index >= 0) state.jobs[index] = job;
          else state.jobs.push(job);
        });
        state.previousJobStates = new Map(state.jobs.map((job) => [job.id, job.status]));
        state.lastQueuedBatch = {
          novelId: novel.id,
          draftId: String(queuedDraft.id || savedDraft.id || ""),
          totalVideos: Number(queued?.total_videos || incoming.length),
          queuedAt: new Date().toISOString(),
        };
        // The queued jobs own a frozen snapshot.  Detach the editor from that
        // draft immediately so the operator can keep creating independent
        // batches while the existing queue continues in the background.
        beginNextProductionBatch(state.productionNovel, queuedDraft, { render: false, focus: false });
        renderJobs();
        renderRecords();
        renderProductionWorkbench();
        startPolling();
        toast(`上一批 ${Number(queued?.total_videos || incoming.length)} 个视频已排队；现在可以继续建立下一批。`);
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function retryJob(button) {
    try {
      await withBusyButton(button, "正在重试…", async () => {
        await checkedCall("retry_failed", button.dataset.retryJob);
        state.jobs = await checkedCall("get_jobs");
        state.previousJobStates = new Map(state.jobs.map((job) => [job.id, job.status]));
        renderJobs();
        if (state.productionNovel) renderProductionWorkbench();
        startPolling();
        toast("任务已从中断点重新加入队列。", "info");
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function statusText(job) {
    const labels = {
      queued: "等待处理",
      preflight: "检查输入",
      preparing: "准备资源",
      polishing: "润色文稿",
      narrating: "生成旁白",
      composing: "编排素材",
      previewing: "准备渲染",
      rendering: "渲染成片",
      waiting_preview: "旧版任务等待继续",
      awaiting_approval: "旧版任务等待继续",
      approved: "等待制作",
      completed: "已完成",
      failed: "生成失败",
      cancelled: "已取消",
      interrupted: "已中断，可重试",
    };
    if (["waiting_preview", "awaiting_approval", "approved"].includes(job.status)) return labels[job.status];
    return job.stage_label || labels[job.status] || job.status;
  }

  function hasPollableWork(jobs = state.jobs) {
    const batches = new Map();
    jobs.filter((job) => !job.archived).forEach((job) => {
      const key = jobBatchKey(job);
      if (!batches.has(key)) batches.set(key, { summary: null, jobs: [] });
      const batch = batches.get(key);
      batch.jobs.push(job);
      if (job.batch_summary && typeof job.batch_summary === "object") {
        batch.summary = job.batch_summary;
      }
    });
    return [...batches.values()].some((batch) => {
      if (batch.summary) {
        return Number(batch.summary.unfinished ?? batch.summary.active ?? 0) > 0;
      }
      return batch.jobs.some((job) => executionStatuses.has(job.status) || ["queued", "approved"].includes(job.status));
    });
  }

  function renderProductionState() {
    const activeJobs = state.jobs.filter((job) => !job.archived);
    const activeBatches = groupJobsByBatch(activeJobs);
    const counts = { queued: 0, active: 0, approval: 0, completed: 0, failed: 0, cancelled: 0, interrupted: 0 };
    activeBatches.forEach((batch) => {
      if (batch.summary) {
        counts.queued += Number(batch.summary.queued || 0);
        counts.active += Number(batch.summary.running || 0);
        counts.approval += Number(batch.summary.approval || 0);
        counts.completed += Number(batch.summary.completed || 0);
        counts.failed += Number(batch.summary.failed || 0);
        counts.cancelled += Number(batch.summary.cancelled || 0);
        counts.interrupted += Number(batch.summary.interrupted || 0);
        return;
      }
      batch.jobs.forEach((job) => {
        if (["queued", "approved"].includes(job.status)) counts.queued += 1;
        else if (["waiting_preview", "awaiting_approval"].includes(job.status)) counts.approval += 1;
        else if (job.status === "completed") counts.completed += 1;
        else if (job.status === "failed") counts.failed += 1;
        else if (job.status === "cancelled") counts.cancelled += 1;
        else if (job.status === "interrupted") counts.interrupted += 1;
        else if (executionStatuses.has(job.status)) counts.active += 1;
      });
    });
    $("#metric-queued").textContent = counts.queued;
    $("#metric-active").textContent = counts.active;
    $("#metric-approval").textContent = counts.interrupted;
    $("#metric-completed").textContent = counts.completed;
    $("#metric-failed").textContent = counts.failed;
    const localTaskCount = $("#production-local-task-count");
    if (localTaskCount) localTaskCount.textContent = `${activeBatches.length} 批 · ${activeJobs.length} 条`;

    const activeJob = activeJobs.find((job) => executionStatuses.has(job.status));
    document.body.classList.toggle("production-resource-busy", Boolean(activeJob));
    const legacyHoldJob = activeJobs.find((job) => job.status === "awaiting_approval")
      || activeJobs.find((job) => job.status === "waiting_preview");
    const tally = $("#queue-tally");
    tally.className = "tally-light";
    let title = "制作台已就绪";
    let copy = "选择小说并完成本批设置，在右侧即时确认后直接生成完整视频。";
    const queueConnection = state.queueConnection && typeof state.queueConnection === "object"
      ? state.queueConnection
      : {};
    if (queueConnection.reconnecting) {
      const retrySeconds = Math.max(0, Math.ceil(Number(queueConnection.retry_in_seconds || 0)));
      tally.classList.add("is-error");
      title = "主机连接暂时中断，正在自动重连";
      copy = retrySeconds
        ? `队列和当前任务不会丢失，系统将在约 ${retrySeconds} 秒后重试。`
        : "队列和当前任务不会丢失，系统正在重新连接主机。";
    } else if (activeJob) {
      const progress = Math.round((Number(activeJob.progress) || 0) * 100);
      tally.classList.add("is-live");
      title = `${activeJob.title} · ${statusText(activeJob)}`;
      copy = `当前 ${progress}% · 后面还有 ${counts.queued} 个待处理任务。`;
    } else if (legacyHoldJob) {
      tally.classList.add("is-ready");
      title = "发现旧版等待任务";
      copy = `${counts.approval} 个历史任务仍处于等待状态；当前批次会直接生成完整视频。`;
    } else if (counts.queued) {
      tally.classList.add("is-ready");
      title = "队列已就绪";
      copy = `${counts.queued} 个任务等待制作，点击“开始”后将依次处理。`;
    } else if (counts.failed || counts.interrupted) {
      tally.classList.add("is-error");
      title = "本轮已结束，有任务需要处理";
      copy = `${counts.completed} 个已完成，${counts.failed} 个失败${counts.interrupted ? `，${counts.interrupted} 个已中断` : ""}；可从任务卡重试。`;
    } else if (activeJobs.length) {
      tally.classList.add("is-done");
      title = "本轮任务已结束";
      copy = `${counts.completed} 个已完成${counts.cancelled ? `，${counts.cancelled} 个已取消` : ""}。`;
    }
    $("#queue-status-title").textContent = title;
    $("#queue-status-copy").textContent = copy;

    const start = $("#start-queue");
    const cancel = $("#cancel-queue");
    const clear = $("#clear-finished");
    start.disabled = counts.queued === 0 || Boolean(activeJob);
    cancel.disabled = counts.active === 0;
    clear.disabled = counts.completed + counts.failed + counts.cancelled + counts.interrupted === 0;
    clear.hidden = state.jobArchiveView === "archived";
    $("#start-queue-label").textContent = activeJob
      ? "正在制作"
      : counts.queued
        ? `开始 ${counts.queued} 个任务`
        : "没有待办";

    const stageOrder = ["script", "voice", "visual", "output"];
    const stageByStatus = {
      queued: "script",
      preflight: "script",
      preparing: "script",
      polishing: "script",
      narrating: "voice",
      composing: "visual",
      previewing: "output",
      rendering: "output",
      waiting_preview: "output",
      awaiting_approval: "output",
      approved: "output",
      completed: "output",
    };
    const reference = activeJob || activeJobs.find((job) => ["queued", "approved"].includes(job.status)) || legacyHoldJob || null;
    const currentStage = reference ? stageByStatus[reference.status] || "script" : "";
    const currentIndex = stageOrder.indexOf(currentStage);
    $$("#production-ribbon [data-stage]").forEach((node, index) => {
      node.classList.toggle("is-current", Boolean(currentStage) && index === currentIndex);
      node.classList.toggle("is-done", Boolean(currentStage) && index < currentIndex);
    });
    if (!reference && activeJobs.length && counts.completed === activeJobs.length) {
      $$("#production-ribbon [data-stage]").forEach((node) => node.classList.add("is-done"));
    }
  }

  function applyArchivedJobPage(page, { append = false } = {}) {
    const items = Array.isArray(page) ? page : Array.isArray(page?.items) ? page.items : [];
    const incoming = items.map((job) => ({ ...job, archived: true }));
    if (append) {
      const merged = new Map(state.archivedJobs.map((job) => [String(job.id), job]));
      incoming.forEach((job) => merged.set(String(job.id), job));
      state.archivedJobs = [...merged.values()];
    } else {
      state.archivedJobs = incoming;
    }
    state.archivedJobsTotal = Array.isArray(page)
      ? Math.max(state.archivedJobsTotal, state.archivedJobs.length)
      : Math.max(0, Number(page?.total ?? state.archivedJobs.length));
    state.archivedJobsLoaded = true;
  }

  async function loadArchivedJobs({ reset = false, render = true } = {}) {
    if (state.archivedJobsLoading) return;
    if (!reset && state.archivedJobsLoaded && state.archivedJobs.length >= state.archivedJobsTotal) return;
    state.archivedJobsLoading = true;
    if (render) renderJobs();
    try {
      const offset = reset ? 0 : state.archivedJobs.length;
      const page = await checkedCall("get_archived_jobs", {
        limit: state.archivedJobsPageSize,
        offset,
      });
      applyArchivedJobPage(page, { append: !reset });
    } finally {
      state.archivedJobsLoading = false;
      if (render) renderJobs();
    }
  }

  async function applyJobMutationResult(result) {
    state.jobs = Array.isArray(result)
      ? result.filter((job) => !job.archived)
      : Array.isArray(result?.current_jobs)
        ? result.current_jobs
        : await checkedCall("get_jobs");
    if (Array.isArray(result?.archived_jobs)) {
      applyArchivedJobPage(
        {
          items: result.archived_jobs,
          total: Number(result.archived_jobs_total ?? result.archived_jobs.length),
        },
      );
    } else {
      await loadArchivedJobs({ reset: true, render: false });
    }
    state.previousJobStates = new Map(state.jobs.map((job) => [job.id, job.status]));
    renderJobs();
  }

  async function archiveJob(button) {
    try {
      await withBusyButton(button, "正在归档…", async () => {
        const result = await checkedCall("archive_job", button.dataset.archiveJob);
        await applyJobMutationResult(result);
        toast("任务已归档，可在“已归档”中随时恢复。", "info");
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function restoreJob(button) {
    try {
      await withBusyButton(button, "正在恢复…", async () => {
        const result = await checkedCall("restore_job", button.dataset.restoreJob);
        await applyJobMutationResult(result);
        toast("任务已恢复到“进行中”视图。", "info");
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function archiveBatch(button) {
    try {
      await withBusyButton(button, "正在归档整批…", async () => {
        const result = await checkedCall("archive_batch", button.dataset.archiveBatch);
        await applyJobMutationResult(result);
        toast("整个批次已归档，视频、配音和错误记录都不会被删除。", "info");
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function restoreBatch(button) {
    try {
      await withBusyButton(button, "正在恢复整批…", async () => {
        const result = await checkedCall("restore_batch", button.dataset.restoreBatch);
        await applyJobMutationResult(result);
        toast("整个批次已恢复到“进行中”视图。", "info");
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function jobBatchKey(job) {
    return String(
      job?.batch_id
      || job?.production_run_id
      || job?.production_draft_id
      || `legacy:${job?.id || "unknown"}`,
    );
  }

  function groupJobsByBatch(jobs) {
    const groups = new Map();
    jobs.forEach((job) => {
      const key = jobBatchKey(job);
      if (!groups.has(key)) {
        groups.set(key, {
          key,
          batchId: String(job.batch_id || ""),
          summary: null,
          jobs: [],
        });
      }
      const batch = groups.get(key);
      batch.jobs.push(job);
      if (job.batch_summary && typeof job.batch_summary === "object") {
        batch.summary = job.batch_summary;
      }
    });
    return [...groups.values()].map((batch) => ({
      ...batch,
      outputFolder: batchResolvedOutputFolder(batch.jobs),
    }));
  }

  function jobBatchCounts(batch) {
    if (batch.summary) {
      return {
        completed: Number(batch.summary.completed || 0),
        failed: Number(batch.summary.failed || 0) + Number(batch.summary.interrupted || 0),
        cancelled: Number(batch.summary.cancelled || 0),
        active: Number(batch.summary.unfinished ?? batch.summary.active ?? 0),
      };
    }
    return batch.jobs.reduce(
      (counts, job) => {
        if (job.status === "completed") counts.completed += 1;
        else if (job.status === "cancelled") counts.cancelled += 1;
        else if (["failed", "interrupted"].includes(job.status)) counts.failed += 1;
        else counts.active += 1;
        return counts;
      },
      { completed: 0, failed: 0, cancelled: 0, active: 0 },
    );
  }

  function jobBatchTotal(batch) {
    return Math.max(
      Number(batch.summary?.total || 0),
      ...batch.jobs.map((job) => Number(job.batch_total_count || 0)),
      batch.jobs.length,
    );
  }

  function isJobBatchExpanded(batch, archivedView) {
    if (!(state.jobBatchDisclosure instanceof Map)) state.jobBatchDisclosure = new Map();
    const disclosureKey = `${archivedView ? "archived" : "active"}:${batch.key}`;
    if (state.jobBatchDisclosure.has(disclosureKey)) {
      return Boolean(state.jobBatchDisclosure.get(disclosureKey));
    }
    return !archivedView && (
      Number(batch.summary?.unfinished ?? batch.summary?.active ?? 0) > 0
      || batch.jobs.some((job) => !terminalStatuses.has(job.status))
    );
  }

  function renderJobs() {
    const root = $("#job-list");
    const activeJobs = state.jobs.filter((job) => !job.archived);
    const archivedJobs = state.archivedJobs;
    const visibleJobs = state.jobArchiveView === "archived" ? archivedJobs : activeJobs;
    const activeBatches = groupJobsByBatch(activeJobs);
    const archivedBatches = groupJobsByBatch(archivedJobs);
    const visibleBatches = state.jobArchiveView === "archived" ? archivedBatches : activeBatches;
    const activeVideoTotal = activeBatches.reduce((total, batch) => total + jobBatchTotal(batch), 0);
    $("#job-active-count").textContent = `${activeBatches.length}批 · ${activeVideoTotal}条`;
    $("#job-archived-count").textContent = `${archivedBatches.length}批 · ${state.archivedJobsTotal}条`;
    $("#queue-count").textContent = state.jobArchiveView === "archived" && state.archivedJobsTotal > archivedJobs.length
      ? `已载入 ${visibleBatches.length} 个批次 · ${archivedJobs.length} / ${state.archivedJobsTotal} 个视频`
      : state.jobArchiveView === "archived"
        ? `${visibleBatches.length} 个批次 · ${state.archivedJobsTotal} 个视频`
        : `${visibleBatches.length} 个批次 · ${activeVideoTotal} 个视频`;
    $$('[data-job-view]').forEach((button) => {
      const selected = button.dataset.jobView === state.jobArchiveView;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    if (!visibleJobs.length) {
      const archivedView = state.jobArchiveView === "archived";
      root.innerHTML = `
        <div class="empty-state queue-empty-state ${archivedView ? "is-archive" : ""}">
          <div class="empty-reel" aria-hidden="true"><i></i><i></i><i></i></div>
          <h3>${archivedView ? "还没有归档任务" : "当前没有进行中的任务"}</h3>
          <p>${archivedView ? "完成、失败、取消或中断的任务归档后会保留在这里，不会删除成片和日志。" : archivedJobs.length ? "已结束任务都已收进归档，可切换到“已归档”查看或恢复。" : state.platforms.length ? "从小说库选择一部小说，完成本批设置后即可开始制作。" : "先建立平台档案，再回来添加小说批次。"}</p>
          ${!archivedView && !state.platforms.length ? '<button type="button" class="button button-secondary empty-action" data-open-view="platforms">创建平台档案</button>' : ""}
        </div>`;
      renderProductionState();
      renderLibraryFailureBanner();
      applyWebCapabilityHints(root);
      return;
    }
    const archivedView = state.jobArchiveView === "archived";
    const cards = visibleBatches
      .map((batch, batchIndex) => {
        const firstJob = batch.jobs[0] || {};
        const platform = state.platforms.find((item) => item.id === firstJob.platform_id);
        const counts = jobBatchCounts(batch);
        const total = jobBatchTotal(batch);
        const batchProgress = batch.summary
          ? Math.round(Math.max(0, Math.min(1, Number(batch.summary.overall_progress) || 0)) * 100)
          : total
          ? Math.round(batch.jobs.reduce((sum, job) => sum + Math.max(0, Math.min(1, Number(job.progress) || 0)), 0) / total * 100)
          : 0;
        const isOpen = isJobBatchExpanded(batch, archivedView);
        const disclosureKey = `${archivedView ? "archived" : "active"}:${batch.key}`;
        const bodyId = `job-batch-${archivedView ? "archived" : "active"}-${batchIndex}`;
        const allTerminal = batch.summary
          ? counts.active === 0
          : batch.jobs.every((job) => terminalStatuses.has(job.status));
        const needsApproval = batch.summary
          ? Number(batch.summary.approval || 0) > 0
          : batch.jobs.some((job) => ["awaiting_approval", "waiting_preview"].includes(job.status));
        const isLive = batch.summary
          ? Number(batch.summary.running || 0) > 0
          : batch.jobs.some((job) => executionStatuses.has(job.status));
        const batchState = allTerminal
          ? counts.failed
            ? "已结束，有失败"
            : counts.cancelled >= total && counts.completed === 0
              ? "已全部取消"
              : counts.cancelled
                ? "已结束，含取消"
                : "已全部完成"
          : needsApproval
            ? "等待确认"
            : isLive
              ? "正在制作"
              : "等待制作";
        const shortBatchId = batch.batchId ? batch.batchId.slice(0, 10) : `旧任务 ${batchIndex + 1}`;
        const batchJobs = batch.jobs.map((job, index) => {
          const stateClass = ["failed", "completed", "cancelled", "interrupted", "awaiting_approval", "waiting_preview"].includes(job.status) ? job.status : "";
          const progress = Math.round((Number(job.progress) || 0) * 100);
          const jobPlatform = state.platforms.find((item) => item.id === job.platform_id);
          const errorDetail = job.status === "failed" && job.message
            ? `<p class="job-error">${escapeHtml(job.message)}${job.error_log ? `<span>${escapeHtml(job.error_log)}</span>` : ""}</p>`
            : "";
        const resolvedOutputFolder = jobResolvedOutputFolder(job);
        const outputAction = job.status === "completed" && resolvedOutputFolder
          ? `<button type="button" class="job-output" data-output-folder="${escapeHtml(resolvedOutputFolder)}">打开输出</button>`
          : "";
        const retryAction = !job.archived && job.job_kind !== "preview" && ["failed", "cancelled", "interrupted"].includes(job.status)
          ? `<button type="button" class="job-output" data-retry-job="${escapeHtml(job.id)}">重试任务</button>`
          : "";
        const archiveAction = batch.batchId
          ? ""
          : job.archived
            ? `<button type="button" class="job-archive-action is-restore" data-restore-job="${escapeHtml(job.id)}">恢复</button>`
            : terminalStatuses.has(job.status)
              ? `<button type="button" class="job-archive-action" data-archive-job="${escapeHtml(job.id)}">归档</button>`
              : "";
          return `
          <article class="job-card job-${escapeHtml(job.status)} ${job.archived ? "is-archived" : ""}" title="${escapeHtml(job.message || "")}">
            <span class="job-index">${String(index + 1).padStart(2, "0")}</span>
            <div class="job-copy">
              <b>${escapeHtml(job.title)}</b>
              <small>口令 ${escapeHtml(job.code)} · ${escapeHtml(jobPlatform?.name || "平台未知")} · ${escapeHtml(pathLeaf(job.source_file))}</small>
              <div class="job-progress" role="progressbar" aria-label="${escapeHtml(job.title)} 进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><i style="--progress:${progress}%"></i></div>
              ${errorDetail}
            </div>
            <div class="job-state-wrap">
              <span class="job-state ${stateClass}">${escapeHtml(statusText(job))}</span>
              <strong>${progress}%</strong>
              ${outputAction}
              ${retryAction}
              ${archiveAction}
            </div>
          </article>`;
        }).join("");
        const batchHeaderActions = [
          batch.batchId && archivedView
            ? `<button type="button" class="job-batch-archive is-restore" data-restore-batch="${escapeHtml(batch.batchId)}">恢复整批</button>`
            : batch.batchId && allTerminal
              ? `<button type="button" class="job-batch-archive" data-archive-batch="${escapeHtml(batch.batchId)}">归档整批</button>`
              : "",
          batch.outputFolder
            ? `<button type="button" class="job-batch-output" data-output-folder="${escapeHtml(batch.outputFolder)}">打开本批文件夹</button>`
            : "",
          batch.batchId
            ? `<button type="button" class="job-batch-records" data-open-job-batch-records="${escapeHtml(batch.batchId)}">查看制作记录</button>`
            : "",
        ].filter(Boolean).join("");
        return `<section class="job-batch ${archivedView ? "is-archived" : ""} ${allTerminal ? "is-terminal" : "is-active"} ${counts.failed ? "has-failure" : ""} ${isOpen ? "is-open" : ""}">
          <header class="job-batch-head">
            <button type="button" class="job-batch-toggle" data-toggle-job-batch="${escapeHtml(disclosureKey)}" aria-expanded="${String(isOpen)}" aria-controls="${bodyId}">
              <span class="job-batch-disclosure" aria-hidden="true"></span>
              <span class="job-batch-identity">
                <small>BATCH · ${escapeHtml(shortBatchId)}</small>
                <b>${escapeHtml(firstJob.title || "未命名小说批次")}</b>
                <em>${escapeHtml(platform?.name || "平台未知")} · 口令 ${escapeHtml(firstJob.code || "未选择")} · ${total} 个视频</em>
              </span>
              <span class="job-batch-counts" aria-label="批次任务统计">
                <i>未完成 <b>${counts.active}</b></i>
                <i class="is-success">完成 <b>${counts.completed}</b></i>
                <i class="is-failed">失败 <b>${counts.failed}</b></i>
                ${counts.cancelled ? `<i>取消 <b>${counts.cancelled}</b></i>` : ""}
              </span>
              <span class="job-batch-overall">
                <small>${escapeHtml(batchState)}</small>
                <b>${batchProgress}%</b>
                <i role="progressbar" aria-label="${escapeHtml(firstJob.title || "小说批次")} 总进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${batchProgress}"><em style="--progress:${batchProgress}%"></em></i>
              </span>
            </button>
            ${batchHeaderActions ? `<div class="job-batch-actions">${batchHeaderActions}</div>` : ""}
          </header>
          <div class="job-batch-body" id="${bodyId}" ${isOpen ? "" : "hidden"}>
            <div class="job-batch-window-note">当前显示 ${batch.jobs.length}/${total} 个视频任务</div>
            ${batchJobs}
          </div>
        </section>`;
      }).join("");
    const hasMoreArchived = state.jobArchiveView === "archived"
      && archivedJobs.length < state.archivedJobsTotal;
    root.innerHTML = cards + (hasMoreArchived
      ? `<div class="archive-load-more"><button type="button" class="button button-secondary" data-load-archived-jobs ${state.archivedJobsLoading ? "disabled" : ""}>${state.archivedJobsLoading ? "正在读取…" : `加载更多（剩余 ${state.archivedJobsTotal - archivedJobs.length}）`}</button></div>`
      : "");
    $$('[data-output-folder]', root).forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await checkedCall("open_output_folder", button.dataset.outputFolder);
        } catch (error) {
          toast(error.message, "error");
        }
      });
    });
    renderProductionState();
    renderLibraryFailureBanner();
    applyWebCapabilityHints(root);
  }

  function renderHealth() {
    const system = effectiveMediaSystem();
    const ttsSystem = effectiveTtsSystem();
    const ffmpegOk = Boolean(system.ffmpeg_ready);
    const remoteWeb = isAuthenticatedHubBrowser();
    const selfCheck = state.localWorkerSelfCheck && typeof state.localWorkerSelfCheck === "object"
      ? state.localWorkerSelfCheck
      : null;
    const issue = remoteWeb ? state.localWorkerIssue : null;
    const workstationReady = selfCheck ? Boolean(selfCheck.ready) : ffmpegOk;
    $("#system-light").className = `status-light ${workstationReady ? "ready" : "error"}`;
    $("#system-title").textContent = workstationReady
      ? remoteWeb ? "当前制作电脑已就绪" : "本地渲染已就绪"
      : issue?.title || (remoteWeb && !state.localWorker ? "未连接本机制作服务" : "制作环境需要处理");
    $("#system-copy").textContent = workstationReady
      ? `${remoteWeb ? "本机编码器" : "编码器"}：${system.recommended_encoder || "自动选择"}`
      : issue?.fix || selfCheck?.summary || (remoteWeb && !state.localWorker
        ? "打开或重新启动当前电脑上的 StoryForge 后重新自检"
        : "请打开服务设置查看自检结果");
    const providers = effectiveTtsProviders();
    const usesLocalVoice = ["local_kokoro", "kokoro", "local"].includes(providers.tts_provider);
    const usesEdgeVoice = ["edge", "edge_tts", "microsoft_edge", "microsoft_edge_tts"].includes(providers.tts_provider);
    const kokoroConfigured = Boolean(ttsSystem.embedded_kokoro_ready || providers.kokoro_endpoint);
    const edgeRuntimeReady = Boolean(ttsSystem.edge_tts_runtime_ready);
    const fallbackRows = [
      [ffmpegOk ? "ok" : "error", "FFmpeg", ffmpegOk ? system.ffmpeg_path : "未检测到"],
      [system.encoders?.length ? "ok" : "error", "H.264 编码", system.encoders?.join(" / ") || "不可用"],
      [
        kokoroConfigured ? "ok" : usesLocalVoice ? "error" : "optional",
        "Kokoro 配音",
        kokoroConfigured
          ? "已配置，可用于本地配音"
          : usesLocalVoice
            ? "当前选择本地配音，请安装组件或填写 HTTP 地址"
            : "当前使用云端配音，本地组件可选",
      ],
      [
        edgeRuntimeReady ? "ok" : usesEdgeVoice ? "error" : "optional",
        "Edge TTS 多语种",
        edgeRuntimeReady
          ? "组件已安装；候选与配音使用时仍需联网"
          : usesEdgeVoice
            ? "当前已选择 Edge TTS，请先安装 requirements.txt 中的组件"
            : "无需 API Key；安装组件后可选",
      ],
      ["ok", `Python ${system.python || ""}`, system.webview_runtime || "桌面运行时"],
    ];
    const checkRows = Array.isArray(selfCheck?.checks)
      ? selfCheck.checks.map((item) => [
          String(item.status || "error"),
          String(item.label || "检查项"),
          [String(item.summary || ""), item.fix ? `处理：${String(item.fix)}` : ""].filter(Boolean).join("；"),
        ])
      : [];
    const rows = checkRows.length ? checkRows : fallbackRows;
    $("#health-list").innerHTML = rows
      .map(
        ([status, title, copy]) => `
          <div class="health-row">
            <span class="health-dot ${status === "ok" ? "" : status}"></span>
            <div><b>${escapeHtml(title)}</b><small>${escapeHtml(copy)}</small></div>
          </div>`,
      )
      .join("");

    const summary = $("#worker-health-summary");
    if (summary) {
      const summaryState = issue ? "error" : selfCheck?.status || (ffmpegOk ? "ready" : "error");
      summary.className = `worker-health-summary is-${summaryState}`;
      summary.innerHTML = issue
        ? `<b>${escapeHtml(issue.title || "当前电脑需要处理")}</b><span>${escapeHtml(issue.message || "")}</span><small>${escapeHtml(issue.fix || "")}</small>`
        : selfCheck
          ? `<b>${escapeHtml(selfCheck.summary || "自检完成")}</b><span>Worker ${escapeHtml(selfCheck.runtime?.app_version || state.localWorker?.runtime?.app_version || "未知")} · 协议 ${escapeHtml(selfCheck.runtime?.worker_protocol_version ?? "未知")}</span>`
          : remoteWeb
            ? `<b>等待本机制作服务</b><span>StoryForge 启动后将自动检查配音、素材、编码器和输出目录。</span>`
            : `<b>${ffmpegOk ? "本地制作环境可用" : "本地制作环境需要处理"}</b><span>${escapeHtml(system.recommended_encoder || "尚未检测到可用编码器")}</span>`;
    }
    const technical = $("#worker-health-technical");
    const details = $("#worker-health-details");
    if (technical && details) {
      const technicalValue = issue
        ? {
            issue_code: issue.code || "worker_error",
            detail: issue.technical || issue.message || "",
            browser_protocol_version: LOCAL_WORKER_PROTOCOL_VERSION,
          }
        : selfCheck
          ? {
              status: selfCheck.status,
              checked_at: selfCheck.checked_at_unix,
              runtime: selfCheck.runtime,
              checks: selfCheck.checks?.map((item) => ({
                key: item.key,
                status: item.status,
                technical: item.technical,
              })),
            }
          : {
              mode: remoteWeb ? "hub-browser" : "desktop",
              ffmpeg_ready: ffmpegOk,
              encoders: system.encoders || [],
            };
      technical.textContent = JSON.stringify(technicalValue, null, 2);
      details.hidden = false;
    }
    renderLocalMaintenanceStatus();
  }

  function normalizedFreeLocalTtsProvider(value) {
    const provider = String(value || "").trim().toLocaleLowerCase().replaceAll("-", "_");
    if (["edge", "edge_tts", "microsoft_edge", "microsoft_edge_tts"].includes(provider)) return "edge_tts";
    if (["local", "kokoro", "local_kokoro", "kokoro_local"].includes(provider)) return "local_kokoro";
    return "";
  }

  function renderLocalMaintenanceStatus() {
    const providers = effectiveTtsProviders();
    const ttsSystem = effectiveTtsSystem();
    const selected = normalizedFreeLocalTtsProvider(providers.tts_provider);
    const select = $("#employee-local-tts-provider");
    if (select && document.activeElement !== select) select.value = selected;

    const kokoroReady = Boolean(ttsSystem.embedded_kokoro_ready || providers.kokoro_endpoint);
    const edgeReady = Boolean(ttsSystem.edge_tts_runtime_ready);
    const readiness = [
      ["#employee-kokoro-readiness", kokoroReady, kokoroReady ? "本地组件或服务可用" : "未检测到组件，可改用 Edge TTS"],
      ["#employee-edge-readiness", edgeReady, edgeReady ? "组件可用，生成时需要联网" : "当前安装缺少 Edge TTS 组件"],
    ];
    readiness.forEach(([selector, ready, copy]) => {
      const row = $(selector);
      if (!row) return;
      row.classList.toggle("is-ready", ready);
      row.classList.toggle("is-error", !ready);
      const detail = row.querySelector("small");
      if (detail) detail.textContent = copy;
    });

    const stateProof = $("#employee-local-tts-state");
    if (stateProof) {
      const ready = selected === "edge_tts" ? edgeReady : selected === "local_kokoro" ? kokoroReady : false;
      stateProof.textContent = !selected ? "请选择免费本机服务" : ready ? "当前可用" : "当前缺少组件";
      stateProof.className = `connection-state ${ready ? "is-ready" : "is-warn"}`;
    }
  }

  function applyProviderAccessMode() {
    const employee = isEmployeeSession();
    const layout = $("#provider-layout");
    layout?.classList.toggle("is-employee-maintenance", employee);
    $("#save-providers")?.classList.toggle("is-hidden", employee);
    const title = $("#providers-page-title");
    const copy = $("#providers-page-copy");
    if (title) title.textContent = employee ? "维护这台制作电脑" : "本地可长期使用，云端按需加速";
    if (copy) copy.textContent = employee
      ? "查看本机 FFmpeg、编码器和配音状态；这里只影响当前电脑，不会修改小说库、口令、团队或主机设置。"
      : "Kokoro 可离线运行；Edge TTS 无需密钥但生成时需联网；软件不会自动开通付费服务或产生扣费。";
    renderLocalMaintenanceStatus();
  }

  async function switchFreeLocalTtsProvider(value) {
    const selected = normalizedFreeLocalTtsProvider(value);
    if (!selected) throw new Error("员工只能切换 Kokoro 或 Edge TTS 免费配音。");
    const result = await checkedCall("set_local_tts_provider", selected);
    const applied = normalizedFreeLocalTtsProvider(result?.tts_provider || selected) || selected;
    if (state.localWorker?.runtime) {
      state.localWorker.runtime = { ...state.localWorker.runtime, ...result, tts_provider: applied };
    }
    state.system = {
      ...(state.system || {}),
      edge_tts_runtime_ready: Boolean(result?.edge_tts_runtime_ready ?? state.system?.edge_tts_runtime_ready),
      embedded_kokoro_ready: Boolean(result?.embedded_kokoro_ready ?? state.system?.embedded_kokoro_ready),
    };
    if (state.settings) {
      state.settings.providers = {
        ...(state.settings.providers || {}),
        tts_provider: applied,
      };
    }
    if (state.productionNovel) {
      const draft = activeDraft(state.productionNovel);
      state.productionNovel.voice_candidates = [];
      draft.voice = { provider: "", voice_id: "", label: "", profile: "" };
    }
    renderProviderStatus();
    renderHealth();
    return applied;
  }

  function renderProviderStatus() {
    const textProvider = $("#text-provider")?.value || "local";
    const localRuntime = localTtsRuntimeSnapshot();
    const voiceProvider = String($("#tts-provider")?.value || localRuntime?.tts_provider || "local_kokoro");
    const textState = $("#text-provider-state");
    const voiceState = $("#voice-provider-state");
    if (textState) {
      const local = textProvider === "local";
      const hasSavedKey = Boolean(state.settings?.providers?.has_text_api_key);
      const ready = local || Boolean($("#text-api-key")?.value) || hasSavedKey || textProvider === "ollama";
      textState.textContent = local ? "本地可用" : ready ? "已配置" : "待填写 Key";
      textState.className = `connection-state ${ready ? "is-ready" : "is-warn"}`;
    }
    if (voiceState) {
      const edgeSelected = ["edge", "edge_tts", "microsoft_edge", "microsoft_edge_tts"].includes(voiceProvider);
      const deepgramSelected = ["deepgram", "deepgram_aura", "aura", "aura_2"].includes(voiceProvider);
      const localSelected = ["local", "kokoro", "local_kokoro", "kokoro_local", "kokoro_http", "kokoro_cli"].includes(voiceProvider);
      const endpointControl = $("#tts-endpoint");
      const keyControl = $("#tts-api-key");
      const kokoroControl = $("#kokoro-endpoint");
      if (endpointControl) endpointControl.disabled = !deepgramSelected;
      if (keyControl) keyControl.disabled = !deepgramSelected;
      if (kokoroControl) kokoroControl.disabled = !localSelected;
      const providers = effectiveTtsProviders();
      const ttsSystem = effectiveTtsSystem();
      const hasSavedKey = Boolean(providers.has_tts_api_key);
      const cloudReady = Boolean($("#tts-api-key")?.value) || hasSavedKey;
      const localReady = Boolean(ttsSystem.embedded_kokoro_ready || providers.kokoro_endpoint || $("#kokoro-endpoint")?.value);
      const edgeReady = Boolean(ttsSystem.edge_tts_runtime_ready);
      const ready = deepgramSelected ? cloudReady : edgeSelected ? edgeReady : localReady;
      voiceState.textContent = edgeSelected
        ? edgeReady ? "组件已安装 · 需联网" : "待安装 Edge TTS"
        : ready ? "已配置" : deepgramSelected ? "待填写 Key" : "待配置本地服务";
      voiceState.className = `connection-state ${ready ? "is-ready" : "is-warn"}`;
    }
    renderLocalMaintenanceStatus();
  }

  function assignValue(selector, value) {
    const control = $(selector);
    if (control && value !== undefined && value !== null) control.value = value;
  }

  function assignChecked(selector, value) {
    const control = $(selector);
    if (control && value !== undefined && value !== null) control.checked = Boolean(value);
  }

  function styleCategoryForControl(controlId) {
    if (controlId === "intro-card-preset") return "intro";
    if (controlId === "subtitle-preset") return "subtitle";
    if (controlId === "code-card-preset") return "code";
    if (controlId === "outro-card-preset") return "outro";
    return "intro";
  }

  function setStyleControlValue(controlId, value) {
    const control = document.getElementById(controlId);
    if (!control) return;
    if (control.type === "checkbox") control.checked = Boolean(value);
    else control.value = String(value);
  }

  function updateStylePresetCards() {
    $$('[data-style-preset-target]').forEach((button) => {
      const select = document.getElementById(button.dataset.stylePresetTarget || "");
      const active = Boolean(select && select.value === button.dataset.stylePresetValue);
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  const styleControlBindings = {
    intro: {
      font_family: ["intro-font"], headline_font_size: ["intro-headline-size"], headline_color: ["intro-headline-color"],
      body_font_size: ["intro-body-size"], body_color: ["intro-body-color"], label_font_size: ["intro-label-size"],
      label_color: ["intro-label-color"], background_color: ["intro-background"], background_opacity: ["intro-opacity", 100],
      border_color: ["intro-border"], border_width: ["intro-border-width"], shadow_opacity: ["intro-shadow-opacity", 100],
      width_percent: ["intro-width"], position_x_percent: ["intro-x"], position_y_percent: ["intro-y"], padding: ["intro-padding"],
      radius: ["intro-radius"], text_alignment: ["intro-alignment"], max_lines: ["intro-max-lines"],
    },
    subtitle: {
      font_family: ["subtitle-font"], font_size: ["subtitle-size"], text_color: ["subtitle-color"], outline_color: ["subtitle-outline"],
      outline_width: ["subtitle-outline-width"], bottom_margin: ["subtitle-margin"], horizontal_margin: ["subtitle-horizontal-margin"],
      max_chars_per_line: ["subtitle-chars"], max_lines: ["subtitle-max-lines"], bold: ["subtitle-bold"], italic: ["subtitle-italic"],
      shadow_width: ["subtitle-shadow-width"], background_color: ["subtitle-background"], background_opacity: ["subtitle-background-opacity", 100],
      alignment: ["subtitle-alignment"], position_x_percent: ["subtitle-position-x"], word_sync_enabled: ["subtitle-word-sync"],
      unread_color: ["subtitle-unread-color"], active_color: ["subtitle-active-color"], read_color: ["subtitle-read-color"],
      pop_scale: ["subtitle-pop-scale"], pop_duration_ms: ["subtitle-pop-duration"], pop_intensity: ["subtitle-pop-intensity", 100],
    },
    code: {
      font_family: ["code-font"], font_size: ["code-size"], text_color: ["code-color"], background_color: ["code-background"],
      opacity: ["code-opacity", 100], top_margin: ["code-margin"], horizontal_margin: ["code-horizontal-margin"], bold: ["code-bold"],
      outline_color: ["code-outline"], outline_width: ["code-outline-width"], alignment: ["code-alignment"],
      position_x_percent: ["code-x"], position_y_percent: ["code-y"], width_percent: ["code-width"], padding: ["code-padding"], radius: ["code-radius"],
    },
    outro: {
      font_family: ["outro-font"], title_font_size: ["outro-title-size"], title_color: ["outro-title-color"],
      body_font_size: ["outro-body-size"], body_color: ["outro-body-color"], code_font_size: ["outro-code-size"], code_color: ["outro-code-color"],
      background_color: ["outro-background"], background_opacity: ["outro-opacity", 100], border_color: ["outro-border"], border_width: ["outro-border-width"],
      width_percent: ["outro-width"], height_percent: ["outro-height"], position_x_percent: ["outro-x"], position_y_percent: ["outro-y"],
      padding: ["outro-padding"], radius: ["outro-radius"], text_alignment: ["outro-alignment"],
    },
  };

  function resolvedStylePreset(category, presetId) {
    const backendKind = { intro: "intro_card", subtitle: "subtitle", code: "code_card", outro: "outro_card" }[category];
    return backendKind ? state.visual_style_presets?.[backendKind]?.[presetId] : null;
  }

  function applyResolvedStyleToControls(category, values) {
    const bindings = styleControlBindings[category] || {};
    Object.entries(values || {}).forEach(([field, raw]) => {
      const binding = bindings[field];
      if (!binding) return;
      const [controlId, multiplier = 1] = binding;
      setStyleControlValue(controlId, typeof raw === "number" ? raw * multiplier : raw);
    });
  }

  function applyStylePreset(category, presetId, { preview = true } = {}) {
    const preset = visualStylePresetCatalog[category]?.[presetId];
    if (!preset) return;
    const selectId = { intro: "intro-card-preset", subtitle: "subtitle-preset", code: "code-card-preset", outro: "outro-card-preset" }[category];
    setStyleControlValue(selectId, presetId);
    const resolved = resolvedStylePreset(category, presetId);
    if (resolved) applyResolvedStyleToControls(category, resolved);
    else Object.entries(preset.values || {}).forEach(([controlId, value]) => setStyleControlValue(controlId, value));
    if (category === "intro") setStyleControlValue("video-template", "platform_story_card");
    updateStylePresetCards();
    if (preview) {
      setStylePreviewScene(category);
      updateStylePreview();
    }
  }

  function setStyleEditorPanel(category) {
    $$('[data-style-panel]').forEach((button) => {
      const active = button.dataset.stylePanel === category;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    $$('[data-style-editor-panel]').forEach((panel) => {
      const active = panel.dataset.styleEditorPanel === category;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
  }

  function setStylePreviewScene(category) {
    const scene = ["intro", "subtitle", "code", "outro"].includes(category) ? category : "intro";
    state.stylePreviewScene = scene;
    const root = $("#style-preview");
    if (root) root.dataset.previewScene = scene;
    $$('[data-style-preview-scene]').forEach((button) => {
      const active = button.dataset.stylePreviewScene === scene;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const title = $("#style-preview-scene-title");
    if (title) title.textContent = { intro: "简介卡", subtitle: "正文字幕", code: "搜索口令", outro: "封面结尾" }[scene];
    const presetId = $("#" + { intro: "intro-card-preset", subtitle: "subtitle-preset", code: "code-card-preset", outro: "outro-card-preset" }[scene])?.value || "";
    const presetTitle = $("#style-preview-preset-title");
    if (presetTitle) {
      const coverEnabled = $("#cover-outro-enabled")?.checked !== false;
      const label = scene === "outro"
        ? coverEnabled
          ? coverAnimationCatalog[normalizedCoverAnimation($("#cover-animation")?.value)]?.label
          : "不使用封面"
        : visualStylePresetCatalog[scene]?.[presetId]?.label || "自定义";
      presetTitle.textContent = `${label} · ${scene === "outro" ? coverEnabled ? "封面铺满全屏" : "正文画面收尾" : "团队默认"}`;
    }
  }

  function updateStyleRangeProofs() {
    const proofs = {
      "intro-opacity-value": ["intro-opacity", "%"],
      "intro-shadow-value": ["intro-shadow-opacity", "%"],
      "intro-width-value": ["intro-width", "%"],
      "intro-x-value": ["intro-x", "%"],
      "intro-y-value": ["intro-y", "%"],
      "subtitle-background-opacity-value": ["subtitle-background-opacity", "%"],
      "subtitle-position-x-value": ["subtitle-position-x", "%"],
      "subtitle-pop-intensity-value": ["subtitle-pop-intensity", "%"],
      "subtitle-pop-scale-value": ["subtitle-pop-scale", "%"],
      "subtitle-pop-duration-value": ["subtitle-pop-duration", " ms"],
      "code-opacity-value": ["code-opacity", "%"],
      "code-width-value": ["code-width", "%"],
      "code-x-value": ["code-x", "%"],
      "code-y-value": ["code-y", "%"],
      "outro-opacity-value": ["outro-opacity", "%"],
      "outro-width-value": ["outro-width", "%"],
      "outro-height-value": ["outro-height", "%"],
      "outro-x-value": ["outro-x", "%"],
      "outro-y-value": ["outro-y", "%"],
    };
    Object.entries(proofs).forEach(([outputId, [inputId, suffix]]) => {
      const output = document.getElementById(outputId);
      const input = document.getElementById(inputId);
      if (output && input) output.textContent = `${input.value}${suffix}`;
    });
  }

  const CUSTOM_STYLE_STORAGE_KEY = "storyforge.visual-style-presets.v1";

  function customStyleStorageKey() {
    if (!isEmployeeSession()) return CUSTOM_STYLE_STORAGE_KEY;
    const userKey = String(
      state.webSession?.user?.id
      || state.webSession?.user?.username
      || "employee",
    ).replace(/[^a-zA-Z0-9_-]/g, "_");
    return `${CUSTOM_STYLE_STORAGE_KEY}.${userKey}`;
  }

  function loadCustomStylePresets() {
    try {
      const storageKey = customStyleStorageKey();
      let stored = window.localStorage.getItem(storageKey);
      // Migrate the workstation's former unscoped quick presets once so an
      // employee does not lose combinations created before account scoping.
      if (stored === null && storageKey !== CUSTOM_STYLE_STORAGE_KEY) {
        stored = window.localStorage.getItem(CUSTOM_STYLE_STORAGE_KEY);
        if (stored !== null) window.localStorage.setItem(storageKey, stored);
      }
      const parsed = JSON.parse(stored || "[]");
      state.customStylePresets = Array.isArray(parsed) ? parsed.filter((item) => item && item.id && item.name && item.settings) : [];
    } catch (_error) {
      state.customStylePresets = [];
    }
    renderCustomStylePresets();
  }

  function persistCustomStylePresets() {
    try {
      window.localStorage.setItem(customStyleStorageKey(), JSON.stringify(state.customStylePresets));
    } catch (_error) {
      toast("这台电脑无法保存个人快捷预设，请检查浏览器或本机存储权限。", "error");
    }
  }

  function renderCustomStylePresets() {
    const select = $("#custom-style-presets");
    if (!select) return;
    const selected = select.value;
    const items = productionPresetManagementItems().filter((item) => item.recipe?.production_settings);
    select.innerHTML = `<option value="">选择已保存方案</option>${items.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(productionPresetScopeLabel(item))}</option>`).join("")}`;
    if (items.some((item) => item.id === selected)) select.value = selected;
    const remove = $("#delete-custom-style-preset");
    const active = items.find((item) => item.id === select.value);
    if (remove) {
      remove.disabled = !active?.deletable;
      remove.textContent = active?.owned_by_current_user ? "删除我的方案" : active?.deletable ? "管理员删除" : "删除";
    }
  }

  function captureVisualStyleSnapshot() {
    const payload = stylePayload();
    return {
      video_template: payload.video_template,
      intro_card_preset: payload.intro_card_preset,
      subtitle_preset: payload.subtitle_preset,
      code_card_preset: payload.code_card_preset,
      outro_card_preset: payload.outro_card_preset,
      caption_mode: payload.caption_mode,
      subtitle_animation: payload.subtitle_animation,
      cover_outro_enabled: payload.cover_outro_enabled,
      cover_animation: payload.cover_animation,
      intro_animation: payload.intro_animation,
      color_grade: payload.color_grade,
      intro_card: structuredClone(payload.intro_card),
      subtitle: structuredClone(payload.subtitle),
      code_card: structuredClone(payload.code_card),
      outro_card: structuredClone(payload.outro_card),
    };
  }

  async function savePersonalStylePreset({ fallbackName = "", stableId = "" } = {}) {
    const input = $("#custom-style-name");
    const typedName = input?.value.trim() || "";
    const name = typedName || fallbackName;
    if (!name) {
      toast("请先填写个人方案名称。", "error");
      input?.focus();
      return null;
    }
    const normalizedName = name.toLocaleLowerCase();
    const localExisting = stableId
      ? state.customStylePresets.find((item) => item.id === stableId)
      : state.customStylePresets.find((item) => item.name.toLocaleLowerCase() === normalizedName);
    const remoteExisting = productionPresetItems().find((item) => (
      item.owned_by_current_user
      && !item.legacy_local
      && String(item.name || "").trim().toLocaleLowerCase() === normalizedName
    ));
    const settings = captureVisualStyleSnapshot();
    const saved = await checkedCall("save_production_preset", {
      ...(remoteExisting?.id ? { id: remoteExisting.id } : {}),
      name,
      description: "在样式工作室保存的个人制作方案；可在制作台直接套用或继续调整。",
      recipe: { production_settings: portableProductionSettings(settings) },
    });
    const item = {
      id: localExisting?.id || stableId || `style-${Date.now()}`,
      name: String(saved?.name || name),
      settings,
      production_preset_id: String(saved?.id || ""),
      updated_at: new Date().toISOString(),
    };
    if (localExisting) Object.assign(localExisting, item);
    else state.customStylePresets.unshift(item);
    persistCustomStylePresets();
    await refreshProductionPresets();
    renderCustomStylePresets();
    if ($("#custom-style-presets")) $("#custom-style-presets").value = String(saved?.id || "");
    if ($("#delete-custom-style-preset")) {
      $("#delete-custom-style-preset").disabled = false;
      $("#delete-custom-style-preset").textContent = "删除我的方案";
    }
    if (input) input.value = "";
    toast(`个人制作方案“${name}”已保存，制作台现在可以直接选择。`, "info");
    return saved;
  }

  function applyCustomStylePreset(presetId) {
    const preset = productionPresetManagementItems().find((item) => item.id === presetId);
    if (!preset) return;
    const settings = preset.recipe?.production_settings || {};
    const original = state.settings;
    state.settings = { ...(state.settings || {}), ...structuredClone(settings) };
    loadSettingsIntoControls();
    state.settings = original;
    const nextAction = state.styleEditingScope === "batch"
      ? "点击“应用到本批”即可使用。"
      : state.styleEditingScope === "personal"
        ? "可继续微调，保存后只属于当前账号。"
        : "点击“保存为团队默认”后才会同步给新批次。";
    toast(`已载入“${preset.name}”；${nextAction}`, "info");
  }

  function updateStyleScopeUI() {
    const batch = state.styleEditingScope === "batch";
    const personal = state.styleEditingScope === "personal";
    const bar = $("#style-scope-bar");
    bar?.classList.toggle("is-batch", batch);
    bar?.classList.toggle("is-personal", personal);
    if ($("#style-scope-mark")) $("#style-scope-mark").textContent = batch ? "THIS BATCH" : personal ? "MY PRESET" : "GLOBAL";
    if ($("#style-scope-title")) $("#style-scope-title").textContent = batch
      ? "正在微调当前制作批次"
      : personal
        ? "正在编辑我的制作方案"
        : "正在编辑团队默认模板";
    if ($("#style-scope-copy")) $("#style-scope-copy").textContent = batch
      ? "普通员工也可以自由修改；应用后只写入本批设置，不影响其他人。"
      : personal
        ? "保存到当前账号后会立即出现在制作台；不会覆盖其他员工的方案。"
        : "只有管理员可以覆盖团队默认；保存后只影响新建批次。";
    if ($("#save-style")) $("#save-style").textContent = batch ? "应用到本批" : personal ? "保存个人制作方案" : "保存为团队默认";
    if ($("#style-back")) $("#style-back").textContent = batch ? "返回制作台" : "返回设置";
    const activeScope = batch ? "batch" : personal ? "personal" : "global";
    $$('[data-style-scope-step]').forEach((item) => item.classList.toggle("is-current", item.dataset.styleScopeStep === activeScope));
    const pageTitle = $("#style-page-title");
    const pageCopy = $("#style-page-copy");
    const shelfTitle = $("#custom-style-shelf-title");
    const shelfCopy = $("#custom-style-shelf-copy");
    if (pageTitle) pageTitle.textContent = personal ? "编辑并保存我的制作方案" : "把常用效果保存成方案，制作台一键套用";
    if (pageCopy) pageCopy.textContent = personal
      ? "先选预设，需要精调时再展开高级设置；保存后会立即出现在制作台。"
      : "管理员可以查看和删除所有员工方案；制作台仍只显示当前账号自己的方案。";
    if (shelfTitle) shelfTitle.textContent = personal ? "我的制作方案" : "方案管理";
    if (shelfCopy) shelfCopy.textContent = personal
      ? "可以保存多套个人方案，之后在制作台直接套用；员工只能删除自己的方案。"
      : "把当前四类画面保存为账号方案；管理员可以查看并删除所有员工方案。";
  }

  function loadVisualSnapshotIntoControls(snapshot) {
    const original = state.settings;
    state.settings = { ...(state.settings || {}), ...structuredClone(snapshot || {}) };
    loadSettingsIntoControls();
    state.settings = original;
  }

  function openGlobalStyleStudio() {
    const personal = isEmployeeSession();
    state.styleEditingScope = personal ? "personal" : "global";
    const personalDefault = personal
      ? state.customStylePresets.find((item) => item.id === "style-personal-default")
      : null;
    if (personalDefault) loadVisualSnapshotIntoControls(personalDefault.settings);
    else loadSettingsIntoControls();
    updateStyleScopeUI();
    navigate("styles");
  }

  function openBatchStyleStudio() {
    const novel = state.productionNovel;
    if (!novel) return;
    syncProductionDraftFromControls();
    const draft = activeDraft(novel);
    state.styleEditingScope = "batch";
    loadVisualSnapshotIntoControls(draft.production_settings || {});
    updateStyleScopeUI();
    navigate("styles");
    window.setTimeout(() => $(".style-editor-tabs button.is-active")?.focus(), 80);
  }

  function applyVisualStyleToCurrentBatch() {
    const novel = state.productionNovel;
    if (!novel) throw new Error("当前没有可应用样式的制作批次。");
    const draft = activeDraft(novel);
    draft.production_settings = { ...(draft.production_settings || {}), ...captureVisualStyleSnapshot() };
    draft.recipe_dirty = true;
    state.styleEditingScope = "global";
    navigate("queue");
    renderProductionWorkbench();
    toast("本批样式已应用。开始完整生成前仍可继续微调。", "info");
  }

  function updateSpeedPresetState() {
    const current = Number($("#setting-wpm")?.value || 0);
    $$("[data-wpm]").forEach((button) => {
      button.classList.toggle("is-active", Number(button.dataset.wpm) === current);
    });
  }

  function loadSettingsIntoControls() {
    const settings = state.settings;
    if (!settings) return;
    const mode = $(`input[name="adult_mode"][value="${settings.adult_mode}"]`);
    if (mode) mode.checked = true;
    assignValue("#setting-wpm", settings.narration_wpm);
    updateSpeedPresetState();
    assignValue("#setting-chapter-pause", settings.chapter_pause_seconds);
    assignValue("#setting-bgm-volume", Math.round((settings.bgm_volume ?? 0.28) * 100));
    const bgmOutput = $("#setting-bgm-volume-value");
    if (bgmOutput) bgmOutput.textContent = `${Math.round((settings.bgm_volume ?? 0.28) * 100)}%`;
    assignValue("#voice-suspense", settings.voice_by_mood?.suspense || "dramatic");
    assignValue("#voice-romance", settings.voice_by_mood?.romance || "warm");
    assignValue("#voice-sad", settings.voice_by_mood?.sad || "calm");
    assignValue("#voice-revenge", settings.voice_by_mood?.revenge || "confident");
    assignValue("#video-template", settings.video_template || "classic");
    assignValue("#intro-card-preset", settings.intro_card_preset || "editorial_white");
    assignValue("#caption-mode", settings.caption_mode || "semantic");
    assignValue("#subtitle-preset", settings.subtitle_preset || "clear_outline");
    assignValue("#code-card-preset", settings.code_card_preset || "brand_pill");
    assignValue("#outro-card-preset", settings.outro_card_preset || "editorial_white");
    assignValue("#subtitle-animation", settings.subtitle_animation || "none");
    assignValue("#render-mode", settings.render_mode || "speed");
    assignValue("#output-fps", settings.output_fps || 60);
    if ($("#preview-output")) $("#preview-output").textContent = `1080 × 1920 · ${Number(settings.output_fps || 60)} FPS`;
    assignValue("#cover-animation", normalizedCoverAnimation(settings.cover_animation));
    assignChecked("#cover-outro-enabled", settings.cover_outro_enabled !== false);
    assignValue("#color-grade", settings.color_grade || "neutral");
    assignValue("#intro-animation", settings.intro_animation || "fade_rise");
    const subtitle = settings.subtitle || {};
    assignValue("#subtitle-font", subtitle.font_family || "Arial");
    assignValue("#subtitle-size", subtitle.font_size ?? 52);
    assignValue("#subtitle-chars", subtitle.max_chars_per_line ?? 28);
    assignValue("#subtitle-max-lines", subtitle.max_lines ?? 3);
    assignValue("#subtitle-margin", subtitle.bottom_margin ?? 310);
    assignValue("#subtitle-horizontal-margin", subtitle.horizontal_margin ?? 180);
    assignValue("#subtitle-color", subtitle.text_color || "#FFFFFF");
    assignValue("#subtitle-outline", subtitle.outline_color || "#101828");
    assignValue("#subtitle-outline-width", subtitle.outline_width ?? 4);
    assignValue("#subtitle-shadow-width", subtitle.shadow_width ?? 4);
    assignValue("#subtitle-background", subtitle.background_color || "#101828");
    assignValue("#subtitle-background-opacity", Math.round(Number(subtitle.background_opacity || 0) * 100));
    assignValue("#subtitle-alignment", subtitle.alignment || "center");
    assignValue("#subtitle-position-x", subtitle.position_x_percent ?? 50);
    assignChecked("#subtitle-bold", subtitle.bold !== false);
    assignChecked("#subtitle-italic", subtitle.italic === true);
    assignChecked("#subtitle-word-sync", subtitle.word_sync_enabled === true);
    assignValue("#subtitle-unread-color", subtitle.unread_color || "#FFFFFF");
    assignValue("#subtitle-active-color", subtitle.active_color || "#FFE06A");
    assignValue("#subtitle-read-color", subtitle.read_color || "#FFFFFF");
    assignValue("#subtitle-pop-scale", Math.round(Number(subtitle.pop_scale || 112)));
    assignValue("#subtitle-pop-duration", subtitle.pop_duration_ms ?? 160);
    assignValue("#subtitle-pop-intensity", Math.round(Number(subtitle.pop_intensity ?? 0.65) * 100));
    const intro = settings.intro_card || {};
    assignValue("#intro-font", intro.font_family || "Arial");
    assignValue("#intro-headline-size", intro.headline_font_size ?? 66);
    assignValue("#intro-headline-color", intro.headline_color || "#FFE06A");
    assignValue("#intro-body-size", intro.body_font_size ?? 32);
    assignValue("#intro-body-color", intro.body_color || "#263247");
    assignValue("#intro-label-size", intro.label_font_size ?? 24);
    assignValue("#intro-label-color", intro.label_color || "#315BD8");
    assignValue("#intro-background", intro.background_color || "#FFFFFF");
    assignValue("#intro-opacity", Math.round(Number(intro.background_opacity ?? 0.98) * 100));
    assignValue("#intro-border", intro.border_color || "#FFFFFF");
    assignValue("#intro-border-width", intro.border_width ?? 2);
    assignValue("#intro-shadow-opacity", Math.round(Number(intro.shadow_opacity ?? 0.28) * 100));
    assignValue("#intro-width", intro.width_percent ?? 65);
    assignValue("#intro-x", intro.position_x_percent ?? 50);
    assignValue("#intro-y", intro.position_y_percent ?? 27);
    assignValue("#intro-padding", intro.padding ?? 40);
    assignValue("#intro-radius", intro.radius ?? 32);
    assignValue("#intro-alignment", intro.text_alignment || "center");
    assignValue("#intro-max-lines", intro.max_lines ?? 5);
    const codeCard = settings.code_card || {};
    assignValue("#code-font", codeCard.font_family || "Arial");
    assignValue("#code-size", codeCard.font_size ?? 42);
    assignValue("#code-margin", codeCard.top_margin ?? 180);
    assignValue("#code-horizontal-margin", codeCard.horizontal_margin ?? 150);
    assignValue("#code-opacity", Math.round(Number(codeCard.opacity ?? 0.92) * 100));
    assignValue("#code-color", codeCard.text_color || "#FFFFFF");
    assignValue("#code-background", codeCard.background_color || "#2446C8");
    assignChecked("#code-bold", codeCard.bold !== false);
    assignValue("#code-outline", codeCard.outline_color || "#FFFFFF");
    assignValue("#code-outline-width", codeCard.outline_width ?? 1);
    assignValue("#code-alignment", codeCard.alignment || "center");
    assignValue("#code-x", codeCard.position_x_percent ?? 50);
    assignValue("#code-y", codeCard.position_y_percent ?? 9);
    assignValue("#code-width", codeCard.width_percent ?? 62);
    assignValue("#code-padding", codeCard.padding ?? 20);
    assignValue("#code-radius", codeCard.radius ?? 12);
    const outro = settings.outro_card || {};
    assignValue("#outro-font", outro.font_family || "Arial");
    assignValue("#outro-title-size", outro.title_font_size ?? 62);
    assignValue("#outro-title-color", outro.title_color || "#17243C");
    assignValue("#outro-body-size", outro.body_font_size ?? 32);
    assignValue("#outro-body-color", outro.body_color || "#53627A");
    assignValue("#outro-code-size", outro.code_font_size ?? 42);
    assignValue("#outro-code-color", outro.code_color || "#315BD8");
    assignValue("#outro-background", outro.background_color || "#FFFFFF");
    assignValue("#outro-opacity", Math.round(Number(outro.background_opacity ?? 0.98) * 100));
    assignValue("#outro-border", outro.border_color || "#D0DAE7");
    assignValue("#outro-border-width", outro.border_width ?? 2);
    assignValue("#outro-width", outro.width_percent ?? 70);
    assignValue("#outro-height", outro.height_percent ?? 38);
    assignValue("#outro-x", outro.position_x_percent ?? 50);
    assignValue("#outro-y", outro.position_y_percent ?? 31);
    assignValue("#outro-padding", outro.padding ?? 42);
    assignValue("#outro-radius", outro.radius ?? 32);
    assignValue("#outro-alignment", outro.text_alignment || "center");
    const providers = settings.providers;
    assignValue("#text-provider", providers.text_provider);
    assignValue("#text-model", providers.text_model);
    assignValue("#text-endpoint", providers.text_endpoint);
    assignValue("#tts-provider", providers.tts_provider);
    assignValue("#tts-endpoint", providers.tts_endpoint);
    assignValue("#kokoro-endpoint", providers.kokoro_endpoint);
    assignValue("#character-limit", providers.monthly_character_limit);
    $("#provider-fallback").checked = Boolean(providers.allow_provider_fallback);
    if (providers.has_text_api_key) $("#text-api-key").placeholder = "已安全保存；留空不修改";
    if (providers.has_tts_api_key) $("#tts-api-key").placeholder = "已安全保存；留空不修改";
    const hub = settings.hub || {};
    assignValue("#hub-mode", hub.mode || "local");
    assignValue("#hub-device-name", hub.device_name || "StoryForge-PC");
    assignValue("#hub-host-device-name", hub.device_name || "StoryForge-PC");
    assignValue("#hub-endpoint", hub.endpoint || "http://127.0.0.1:8765");
    assignValue("#hub-account-username", hub.account_username || "");
    assignValue("#hub-account-password", "");
    assignValue("#hub-listen-port", hub.listen_port || 8765);
    assignChecked("#auto-update-enabled", hub.auto_update_enabled !== false);
    assignChecked("#auto-download-updates", hub.auto_download_updates !== false);
    assignValue("#update-check-minutes", hub.update_check_minutes || 2);
    updateHubModeHelp();
    updateProviderHints();
    renderProviderStatus();
    renderHealth();
    applyProviderAccessMode();
    updateStylePresetCards();
    updateStyleRangeProofs();
    setStylePreviewScene(state.stylePreviewScene || "intro");
    updateStylePreview();
    syncManagedConfigControlsFromSettings();
    renderDeviceSyncStatus();
  }

  function hubModeLabel(mode) {
    return {
      local: "单机使用",
      host: "团队主电脑",
      client: "已接入主电脑",
    }[mode] || "单机使用";
  }

  function updateHubModeHelp() {
    const mode = $("#hub-mode")?.value || "local";
    const help = $("#hub-mode-help");
    const endpoint = $("#hub-endpoint");
    const port = $("#hub-listen-port");
    const username = $("#hub-account-username");
    const password = $("#hub-account-password");
    if (help) help.textContent = mode === "host"
      ? "这台电脑集中保存团队资料；保存重启后必须保持 StoryForge 运行，关闭后制作电脑会断开。"
      : mode === "client"
        ? "输入员工账号和密码即可登录；主电脑地址与电脑名称会自动使用已配置内容。"
        : "适合单台电脑使用，所有记录和文件都留在本机。";
    if (endpoint) endpoint.disabled = mode !== "client";
    if (port) port.disabled = mode !== "host";
    if (username) username.disabled = mode !== "client";
    if (password) password.disabled = mode !== "client";
    $("#hub-port-field")?.classList.toggle("is-hidden", mode !== "host");
    $("#hub-account-connect-card")?.classList.toggle("is-hidden", mode !== "client");
    $("#update-host-panel")?.classList.toggle("is-hidden", mode !== "host");
    $("#managed-device-fleet")?.classList.toggle("is-hidden", mode !== "host");
    $("#device-sync-card")?.classList.toggle("is-hidden", mode !== "client");
    $("#save-hub-settings")?.classList.toggle("is-hidden", mode === "client");
    const card = $(".hub-settings-card");
    if (card) card.dataset.mode = mode;
    renderManagedDeviceWorkspace();
    renderDeviceSyncStatus();
  }

  function hubSettingsPayload() {
    const mode = $("#hub-mode")?.value || "local";
    const deviceName = mode === "client"
      ? $("#hub-device-name")?.value.trim()
      : $("#hub-host-device-name")?.value.trim();
    const hub = {
      mode,
      device_name: deviceName || state.settings?.hub?.device_name || "StoryForge-PC",
      endpoint: $("#hub-endpoint")?.value.trim() || "http://127.0.0.1:8765",
      account_username: $("#hub-account-username")?.value.trim() || "",
      listen_host: "0.0.0.0",
      listen_port: Number($("#hub-listen-port")?.value || 8765),
      // Compatibility fields stay false; production media never uploads.
      share_previews: false,
      share_narration: false,
      auto_update_enabled: Boolean($("#auto-update-enabled")?.checked),
      auto_download_updates: Boolean($("#auto-download-updates")?.checked),
      update_check_minutes: Number($("#update-check-minutes")?.value || 1),
    };
    return { hub };
  }

  function renderHubStatus(data) {
    const restartRequired = Boolean(data?.restart_required);
    const runtimeMode = data?.runtime_mode || data?.mode || state.settings?.hub?.mode || "local";
    const ready = !restartRequired && (runtimeMode === "host"
      ? Boolean(data?.running)
      : runtimeMode === "client"
        ? Boolean(data?.online && data?.connected)
        : Boolean(data?.online || data?.status === "ready" || runtimeMode === "local"));
    const endpoint = String(data?.endpoint || "").trim();
    const hostRunning = !restartRequired && runtimeMode === "host" && Boolean(data?.running);
    const mark = $("#hub-status-mark");
    if (mark) mark.className = `hub-status-mark ${ready ? "is-ready" : "is-warn"}`;
    if ($("#hub-status-title")) $("#hub-status-title").textContent = restartRequired ? "等待重启生效" : ready ? "当前状态正常" : "尚未连接";
    if ($("#hub-status-copy")) {
      $("#hub-status-copy").textContent = runtimeMode === "host"
        ? hostRunning
          ? "主电脑服务已启动。请保持 StoryForge 运行；关闭软件后，制作电脑会断开。"
          : data?.error || "主电脑服务尚未启动。保存并重启 StoryForge 后再检查。"
        : data?.message || (ready ? "这台电脑已经按当前模式准备好。" : "请检查账号密码；如仍无法登录，再打开连接设置检查主电脑地址。");
    }
    if ($("#hub-status-device")) $("#hub-status-device").textContent = data?.device_name || state.settings?.hub?.device_name || "—";
    if ($("#hub-status-mode")) $("#hub-status-mode").textContent = hubModeLabel(data?.mode || state.settings?.hub?.mode);
    const connectionProof = $("#hub-host-connection");
    const endpointCode = $("#hub-host-endpoint");
    const copyEndpoint = $("#copy-hub-endpoint");
    connectionProof?.classList.toggle("is-hidden", runtimeMode !== "host");
    if (endpointCode) endpointCode.textContent = hostRunning && endpoint ? endpoint : "启动主电脑服务后显示";
    if (copyEndpoint) {
      copyEndpoint.disabled = !hostRunning || !endpoint;
      copyEndpoint.dataset.endpoint = hostRunning ? endpoint : "";
    }
    const clientWebProof = $("#hub-client-web-connection");
    const clientWebCode = $("#hub-client-web-endpoint");
    const copyClientWeb = $("#copy-hub-client-web-endpoint");
    const clientWebUrl = String(data?.client_web_url || "").trim();
    const clientWebReady = !restartRequired && runtimeMode === "client" && Boolean(data?.client_web_running && clientWebUrl);
    clientWebProof?.classList.toggle("is-hidden", runtimeMode !== "client");
    if (clientWebCode) clientWebCode.textContent = clientWebReady ? clientWebUrl : "连接并重启后自动显示";
    if (copyClientWeb) {
      copyClientWeb.disabled = !clientWebReady;
      copyClientWeb.dataset.endpoint = clientWebReady ? clientWebUrl : "";
    }
    if (data?.device_sync) state.deviceSyncStatus = data.device_sync;
    renderDeviceSyncStatus();
  }

  async function checkHubStatus(button) {
    try {
      await withBusyButton(button, "正在检查…", async () => {
        const data = await checkedCall("get_hub_status");
        state.hubRuntimeStatus = data;
        renderHubStatus(data);
        await refreshHubDeviceWorkspace({ silent: true });
      });
    } catch (error) {
      state.hubRuntimeStatus = {
        ...(state.hubRuntimeStatus || {}),
        status: "offline",
        connected: false,
        running: false,
        message: error.message,
      };
      renderHubStatus(state.hubRuntimeStatus);
      renderManagedDeviceWorkspace();
      renderDeviceSyncStatus();
      toast(error.message, "error");
    }
  }

  function setHubAccountConnectStatus(message, stateName = "idle") {
    const status = $("#hub-account-connect-status");
    if (!status) return;
    status.textContent = message;
    status.dataset.state = stateName;
  }

  function showWorkerAutostartNotice(payload) {
    const notice = (payload?.notices || []).find((item) => item?.code === "worker_autostart_failed");
    const workerStatus = payload?.worker_autostart || {};
    if (!notice && workerStatus.state !== "warning") return false;
    const message = notice?.message || workerStatus.message || "账号已登录，但本机制作服务未能自动启用。";
    const fix = notice?.fix || workerStatus.fix || "请关闭后重新打开 StoryForge。";
    toast(`${message} ${fix}`.trim(), "error");
    return true;
  }

  async function connectHubWithPassword(button) {
    const endpoint = $("#hub-endpoint")?.value.trim()
      || state.settings?.hub?.endpoint
      || (/^https?:$/i.test(window.location.protocol) ? window.location.origin : "")
      || "http://127.0.0.1:8765";
    const username = $("#hub-account-username")?.value.trim() || "";
    const passwordInput = $("#hub-account-password");
    const password = passwordInput?.value || "";
    const deviceName = $("#hub-device-name")?.value.trim()
      || state.settings?.hub?.device_name
      || state.localWorker?.deviceName
      || "StoryForge-PC";
    if (!username || !password) {
      throw new Error("请输入员工账号和密码。");
    }
    setHubAccountConnectStatus("正在验证账号并登记这台电脑…", "working");
    try {
      await withBusyButton(button, "正在连接…", async () => {
        const data = await checkedCall(
          "connect_hub_with_password",
          endpoint,
          username,
          password,
          deviceName,
        );
        if (data?.settings) state.settings = data.settings;
        state.hubRuntimeStatus = data;
        state.deviceSyncStatus = data?.device_sync || null;
        loadSettingsIntoControls();
        renderHubStatus(data);
        const workerAutostartWarning = data?.worker_autostart?.state === "warning";
        setHubAccountConnectStatus(
          workerAutostartWarning
            ? `已作为 ${data?.member?.display_name || data?.member?.username || username} 登录；请重新打开 StoryForge 以启用本机制作服务。`
            : `已作为 ${data?.member?.display_name || data?.member?.username || username} 登录，本机制作服务已自动启用。`,
          workerAutostartWarning ? "error" : "ready",
        );
        await loadLibraryBootstrap();
        await loadDeviceSyncStatus({ silent: true });
        if (!showWorkerAutostartNotice(data)) {
          toast("登录成功。这台电脑已自动登记，以后直接使用即可。", "info");
        }
      });
    } finally {
      if (passwordInput) passwordInput.value = "";
    }
  }

  function normalizeUpdateStatusResponse(data) {
    return data?.update_status || data || {};
  }

  function formatUpdateTime(value) {
    if (!value) return "尚未检查";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function renderUpdateStatus(data = state.updateStatus || {}) {
    const status = normalizeUpdateStatusResponse(data);
    state.updateStatus = status;
    const stateName = String(status.state || "idle");
    const available = String(status.available_version || status.published_update?.version || "");
    const downloaded = Boolean(status.downloaded || ["downloaded", "scheduled", "deferred", "applying_on_restart"].includes(stateName));
    const scheduled = Boolean(status.apply_on_restart || status.restart_required || ["scheduled", "deferred", "applying_on_restart"].includes(stateName));
    const busy = Boolean(status.rendering_busy || stateName === "deferred");
    const badge = $("#update-state-badge");
    const label = $("#update-state-label");
    const labels = {
      idle: "等待检查",
      checking: "正在检查",
      up_to_date: "已经是最新版",
      available: "发现新版本",
      downloading: "正在下载",
      downloaded: "等待安排安装",
      scheduled: "重启后安装",
      deferred: "渲染结束后安装",
      applying_on_restart: "等待安全退出",
      required: "必须更新",
      error: "更新检查失败",
      published: "新版已发布",
      publisher_idle: "尚未发布新版",
    };
    if (label) label.textContent = labels[stateName] || "更新状态";
    if (badge) {
      badge.className = "update-state-badge";
      if (["available", "downloaded", "scheduled", "published"].includes(stateName)) badge.classList.add("is-available");
      else if (["error", "deferred", "required"].includes(stateName)) badge.classList.add("is-warn");
      else if (["up_to_date", "publisher_idle"].includes(stateName)) badge.classList.add("is-ready");
    }
    if ($("#update-current-version")) $("#update-current-version").textContent = status.current_version || "—";
    if ($("#update-available-version")) $("#update-available-version").textContent = available || "暂无新版";
    if ($("#update-checked-at")) $("#update-checked-at").textContent = formatUpdateTime(status.checked_at || status.published_at);
    if ($("#update-apply-proof")) $("#update-apply-proof").textContent = busy ? "任务结束后自动更新" : scheduled ? "退出后自动安装" : downloaded ? "下载完成，自动安排" : "退出后安全安装";
    const message = $("#update-message");
    if (message) {
      message.textContent = status.error || status.message || "连接主电脑后会自动读取可用版本。";
      message.className = `update-message ${["error", "required"].includes(stateName) ? "is-warn" : ["up_to_date", "downloaded", "scheduled", "published"].includes(stateName) ? "is-ready" : ""}`;
    }
    const checkButton = $("#check-for-updates");
    if (checkButton) checkButton.disabled = ["checking", "downloading"].includes(stateName);
    const downloadButton = $("#download-update");
    downloadButton?.classList.toggle("is-hidden", !available || downloaded || ["published", "publisher_idle"].includes(stateName));
    const scheduleButton = $("#schedule-update");
    scheduleButton?.classList.toggle("is-hidden", !downloaded || scheduled);
    if (scheduleButton) {
      scheduleButton.disabled = busy;
      scheduleButton.textContent = busy ? "渲染结束后可安装" : "重启后安装";
    }
    $("#cancel-scheduled-update")?.classList.toggle("is-hidden", !scheduled);
    if ($("#update-publish-version") && available && ["published", "publisher_idle"].includes(stateName)) $("#update-publish-version").value = available;
    renderEmployeeUpdateStatus(status, {
      stateName,
      available,
      downloaded,
      scheduled,
      busy,
    });
  }

  function renderEmployeeUpdateStatus(status, derived = {}) {
    const installedDesktop = hasDesktopBridge();
    const stateName = String(derived.stateName || status?.state || "idle");
    const available = String(derived.available || status?.available_version || status?.published_update?.version || "");
    const downloaded = derived.downloaded ?? Boolean(status?.downloaded || ["downloaded", "scheduled", "deferred", "applying_on_restart"].includes(stateName));
    const scheduled = derived.scheduled ?? Boolean(status?.apply_on_restart || status?.restart_required || ["scheduled", "deferred", "applying_on_restart"].includes(stateName));
    const busy = derived.busy ?? Boolean(status?.rendering_busy || stateName === "deferred");
    const current = String(status?.current_version || "—");
    const trigger = $("#open-employee-update");
    const triggerLabel = $("#employee-update-trigger-label");
    const triggerLabels = {
      checking: "正在检查",
      available: "发现新版本",
      downloading: "正在下载",
      downloaded: "可以安装",
      scheduled: "等待重启",
      deferred: "等待任务结束",
      applying_on_restart: "正在安装",
      up_to_date: "已是最新版",
      required: "必须更新",
      error: "检查失败",
    };
    if (triggerLabel) triggerLabel.textContent = installedDesktop ? (triggerLabels[stateName] || "检查版本") : (available ? "下载客户端" : "检查客户端");
    if (trigger) {
      trigger.classList.remove("is-available", "is-ready", "is-warn");
      if (["available", "downloaded", "scheduled", "downloading"].includes(stateName)) trigger.classList.add("is-available");
      else if (stateName === "up_to_date") trigger.classList.add("is-ready");
      else if (["error", "deferred", "required"].includes(stateName)) trigger.classList.add("is-warn");
      trigger.title = status?.message || status?.error || "查看这台电脑的软件更新";
    }
    if ($("#employee-update-current-version")) $("#employee-update-current-version").textContent = current;
    if ($("#employee-update-current-label")) $("#employee-update-current-label").textContent = installedDesktop ? "当前版本" : "当前 Hub 版本";
    if ($("#employee-update-current-copy")) $("#employee-update-current-copy").textContent = installedDesktop ? "这台电脑正在使用" : "网页正在连接的主电脑";
    if ($("#employee-update-available-version")) $("#employee-update-available-version").textContent = available || (stateName === "up_to_date" ? current : "暂无新版本");
    if ($("#employee-update-checked-at")) $("#employee-update-checked-at").textContent = formatUpdateTime(status?.checked_at || status?.published_at);
    const message = $("#employee-update-message");
    if (message) {
      message.textContent = status?.error || status?.message || "连接主电脑后即可检查团队发布的新版本。";
      message.className = `update-message employee-update-message ${["error", "required"].includes(stateName) ? "is-warn" : ["up_to_date", "downloaded", "scheduled"].includes(stateName) ? "is-ready" : ""}`;
    }
    const releaseNotes = String(status?.release_notes || status?.published_update?.release_notes || "").trim();
    $("#employee-update-release")?.classList.toggle("is-hidden", !releaseNotes);
    if ($("#employee-update-release-notes")) $("#employee-update-release-notes").textContent = releaseNotes;
    if ($("#employee-update-safety-copy")) {
      $("#employee-update-safety-copy").textContent = stateName === "required"
        ? "当前版本过旧，不能领取新的制作任务。已在生成的任务不受影响；请检查更新，安装后重新打开 StoryForge。"
        : !installedDesktop
        ? "下载完成后退出旧版 StoryForge，解压到新文件夹并双击 StoryForge Studio.exe。"
        : busy
        ? "当前视频仍在生成；安装按钮已暂停，任务结束后再重启即可。"
        : scheduled
          ? "更新已校验并安排完成；可以现在重启安装，也可以正常关闭后自动安装。"
          : downloaded
            ? "更新包已经校验；可选择稍后关闭时安装，或现在重启安装。"
            : "更新包会先完成校验；正在生成视频时不会强制退出。";
    }
    const checking = ["checking", "downloading"].includes(stateName);
    const checkButton = $("#employee-check-for-updates");
    if (checkButton) {
      checkButton.disabled = checking;
      checkButton.textContent = installedDesktop ? (stateName === "required" ? "检查并获取更新" : "检查更新") : "检查发布版本";
    }
    const downloadButton = $("#employee-download-update");
    downloadButton?.classList.toggle("is-hidden", !available || (installedDesktop && (downloaded || ["published", "publisher_idle", "up_to_date"].includes(stateName))));
    if (downloadButton) downloadButton.textContent = installedDesktop ? "下载更新" : "下载客户端更新包";
    $("#employee-schedule-update")?.classList.toggle("is-hidden", !installedDesktop || !downloaded || scheduled);
    const scheduleButton = $("#employee-schedule-update");
    if (scheduleButton) scheduleButton.disabled = busy;
    $("#employee-cancel-scheduled-update")?.classList.toggle("is-hidden", !installedDesktop || !scheduled);
    const restartButton = $("#employee-restart-update");
    restartButton?.classList.toggle("is-hidden", !installedDesktop || !downloaded);
    if (restartButton) {
      restartButton.disabled = busy;
      restartButton.textContent = busy ? "任务结束后可重启" : "立即重启安装";
    }
  }

  async function loadBrowserUpdateStatus() {
    const result = await webRequest("/web/api/update");
    if (!result?.ok) throw new Error(result?.error || "暂时无法读取主电脑发布的客户端更新。");
    const published = result.data || {};
    state.browserUpdateDownloadUrl = String(published.download_url || "");
    const available = Boolean(published.available && published.version && state.browserUpdateDownloadUrl);
    const status = {
      state: available ? "available" : "up_to_date",
      current_version: String(published.hub_version || "—"),
      available_version: available ? String(published.version) : "",
      checked_at: new Date().toISOString(),
      published_at: String(published.published_at || ""),
      release_notes: String(published.release_notes || ""),
      message: String(published.message || (available ? `可以下载 StoryForge ${published.version} 客户端更新包。` : "主电脑当前没有发布客户端更新。")),
      downloaded: false,
      apply_on_restart: false,
      rendering_busy: false,
    };
    renderUpdateStatus(status);
    return status;
  }

  function closeEmployeeUpdateDialog() {
    const dialog = $("#employee-update-dialog");
    if (dialog?.open && dialog.close) dialog.close();
    else dialog?.removeAttribute("open");
  }

  async function openEmployeeUpdateDialog() {
    const dialog = $("#employee-update-dialog");
    if (!dialog) return;
    renderUpdateStatus(state.updateStatus || {});
    if (dialog.showModal) dialog.showModal();
    else dialog.setAttribute("open", "");
    window.setTimeout(() => $("#employee-check-for-updates")?.focus(), 30);
    try {
      if (hasDesktopBridge()) {
        const status = await checkedCall("get_update_status");
        renderUpdateStatus(status);
      } else {
        await loadBrowserUpdateStatus();
      }
    } catch (error) {
      const message = $("#employee-update-message");
      if (message) {
        message.textContent = error.message || "暂时无法读取更新状态，请稍后重试。";
        message.className = "update-message employee-update-message is-warn";
      }
    }
  }

  async function runUpdateAction(button, busyLabel, method, successMessage = "") {
    try {
      await withBusyButton(button, busyLabel, async () => {
        const result = await checkedCall(method);
        renderUpdateStatus(result);
        if (successMessage) toast(successMessage, "info");
      });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function updateProviderHints() {
    const provider = $("#text-provider").value;
    const endpoint = $("#text-endpoint");
    const help = $("#text-endpoint-help");
    if (provider === "cloudflare") {
      endpoint.placeholder = "https://api.cloudflare.com/client/v4/accounts/.../ai/run/{model}";
      help.textContent = "Cloudflare 必填完整推理地址，需包含 Account ID；可用 {model} 占位。";
    } else if (provider === "ollama") {
      endpoint.placeholder = "留空使用 http://127.0.0.1:11434/api/chat";
      help.textContent = "Ollama 地址可留空使用本机默认值。";
    } else if (provider === "groq") {
      endpoint.placeholder = "留空使用 Groq 官方地址";
      help.textContent = "Groq 地址和模型都可留空使用内置默认值。";
    } else {
      endpoint.placeholder = "本地规则模式不需要 API 地址";
      help.textContent = "本地规则模式无需配置。";
    }
    renderProviderStatus();
  }

  function colorWithOpacity(hex, opacity) {
    const clean = String(hex).replace("#", "");
    if (!/^[0-9a-f]{6}$/i.test(clean)) return `rgba(36,70,200,${opacity})`;
    const values = [0, 2, 4].map((index) => parseInt(clean.slice(index, index + 2), 16));
    return `rgba(${values[0]},${values[1]},${values[2]},${opacity})`;
  }

  function previewTextAdvance(text, fontSize) {
    return Array.from(String(text || "")).reduce((sum, character) => {
      if (/\s/u.test(character)) return sum + fontSize * 0.3;
      if (/[\u2e80-\u9fff\u3040-\u30ff\uac00-\ud7af]/u.test(character)) return sum + fontSize;
      if (/[MW@#%]/u.test(character)) return sum + fontSize * 0.76;
      if (/[A-Z0-9]/u.test(character)) return sum + fontSize * 0.61;
      if (/[,.'’!?;:()\-]/u.test(character)) return sum + fontSize * 0.3;
      return sum + fontSize * 0.52;
    }, 0);
  }

  function estimateSubtitleBlockWidth(text, subtitle, safeWidth) {
    const compact = String(text || "Caption preview").replace(/\s+/g, " ").trim();
    const fontSize = Math.max(32, Math.min(80, Number(subtitle.font_size || 52)));
    const maxChars = Math.max(16, Math.min(40, Number(subtitle.max_chars_per_line || 28)));
    const maxLines = Math.max(1, Math.min(4, Number(subtitle.max_lines || 3)));
    const totalAdvance = previewTextAdvance(compact, fontSize);
    const targetLineWidth = Math.min(safeWidth * 0.9, maxChars * fontSize * 0.54);
    const lineCount = Math.max(1, Math.min(maxLines, Math.ceil(totalAdvance / Math.max(1, targetLineWidth))));
    const longestWord = compact.split(/\s+/u).reduce(
      (widest, word) => Math.max(widest, previewTextAdvance(word, fontSize)),
      0,
    );
    const alignmentAllowance = subtitle.alignment === "center" ? 0 : fontSize * 0.35;
    const balancedWidth = totalAdvance / lineCount * 1.08 + fontSize * 0.9 + alignmentAllowance;
    const minimumReadableWidth = Math.min(safeWidth, fontSize * 5.4);
    return Math.max(
      minimumReadableWidth,
      Math.min(safeWidth, Math.max(longestWord + fontSize, balancedWidth)),
    );
  }

  function normalizedSubtitlePreviewLayout(subtitle = {}, sampleText = "") {
    // Both live previews use the renderer's 1080x1920 reference rails. Unlike
    // the old fixed-width box, the block is estimated from the visible sample,
    // font size and wrapping rules, so a requested X position can move while
    // the final edges remain inside the TikTok-safe area.
    const canvasWidth = 1080;
    const hardHorizontalMargin = 188;
    const horizontalMargin = Math.max(
      hardHorizontalMargin,
      Math.min(canvasWidth / 3, Number(subtitle.horizontal_margin || 180)),
    );
    const safeWidth = canvasWidth - horizontalMargin * 2;
    const blockWidth = estimateSubtitleBlockWidth(sampleText, subtitle, safeWidth);
    const requestedX = Math.max(10, Math.min(90, Number(subtitle.position_x_percent ?? 50)));
    const halfWidth = blockWidth / 2;
    const minimumCenter = horizontalMargin + halfWidth;
    const maximumCenter = canvasWidth - horizontalMargin - halfWidth;
    const centerX = Math.max(minimumCenter, Math.min(maximumCenter, requestedX * canvasWidth / 100));
    const bottomMargin = Math.max(360, Math.min(960, Number(subtitle.bottom_margin || 310)));
    return {
      leftPercent: centerX / canvasWidth * 100,
      widthPercent: blockWidth / canvasWidth * 100,
      bottomPercent: bottomMargin / 1920 * 100,
      blockWidth,
    };
  }

  function subtitlePreviewTokens(value) {
    if (/\s/u.test(value)) return value.match(/\S+|\s+/gu) || [];
    if (typeof Intl?.Segmenter === "function") {
      const pieces = [];
      [...new Intl.Segmenter(undefined, { granularity: "word" }).segment(value)].forEach((item) => {
        const segment = String(item.segment || "");
        if (!segment) return;
        if (item.isWordLike === false && pieces.length) pieces[pieces.length - 1] += segment;
        else pieces.push(segment);
      });
      if (pieces.length) return pieces;
    }
    return Array.from(value);
  }

  function setSubtitlePreviewText(element, text, wordMode = "off") {
    const value = String(text || "").replace(/\s+/g, " ").trim();
    const normalizedWordMode = wordMode === true
      ? "cumulative"
      : new Set(["cumulative", "single"]).has(wordMode)
        ? wordMode
        : "off";
    if (normalizedWordMode === "off") {
      element.textContent = value;
      return;
    }
    const tokens = subtitlePreviewTokens(value);
    const words = tokens.filter((token) => !/^\s+$/u.test(token));
    const currentIndex = Math.min(2, Math.max(0, words.length - 1));
    if (normalizedWordMode === "single") {
      element.textContent = words[currentIndex] || value;
      return;
    }
    let wordIndex = 0;
    element.replaceChildren(...tokens.map((token) => {
      if (/^\s+$/u.test(token)) return document.createTextNode(token);
      const span = document.createElement("span");
      span.className = `caption-word ${wordIndex < currentIndex ? "is-read" : wordIndex === currentIndex ? "is-current" : ""}`.trim();
      span.textContent = token;
      wordIndex += 1;
      return span;
    }));
  }

  function applySubtitlePreviewStyles(element, root, subtitle = {}, options = {}) {
    if (!element) return;
    const preset = options.preset || "clear_outline";
    const animation = options.animation || "none";
    const wordMode = new Set(["cumulative", "single"]).has(options.wordMode)
      ? options.wordMode
      : options.wordSync === true
        ? "cumulative"
        : "off";
    const wordSync = wordMode !== "off";
    const sampleText = String(options.text || element.textContent || "Caption preview").trim();
    const isOutro = element.classList.contains("outro-caption-preview");
    setSubtitlePreviewText(element, sampleText, wordMode);
    element.className = `preview-subtitle ${isOutro ? "outro-caption-preview " : ""}preset-${preset} animation-${animation} ${wordSync ? `is-word-sync is-word-${wordMode}` : ""}`.trim();
    element.style.fontFamily = subtitle.font_family || "Arial";
    element.style.fontSize = `${Number(subtitle.font_size || 52) / 4.7}px`;
    element.style.fontWeight = subtitle.bold === false ? "500" : "800";
    element.style.fontStyle = subtitle.italic === true ? "italic" : "normal";
    element.style.textAlign = subtitle.alignment || "center";
    const layout = normalizedSubtitlePreviewLayout(
      subtitle,
      wordMode === "single" ? element.textContent : sampleText,
    );
    element.style.left = `${layout.leftPercent}%`;
    element.style.right = "auto";
    element.style.width = `${layout.widthPercent}%`;
    element.style.maxWidth = `${layout.widthPercent}%`;
    element.style.bottom = `${layout.bottomPercent}%`;
    element.style.transform = "translateX(-50%)";
    const outline = subtitle.outline_color || "#101828";
    const outlineWidth = Math.max(0, Number(subtitle.outline_width || 4) / 3.4);
    const shadowWidth = Math.max(0, Number(subtitle.shadow_width || 4) / 2.6);
    element.style.removeProperty("color");
    element.style.removeProperty("text-shadow");
    element.style.removeProperty("background");
    element.style.setProperty("--subtitle-preview-color", subtitle.text_color || "#FFFFFF");
    element.style.setProperty(
      "--subtitle-preview-background",
      colorWithOpacity(subtitle.background_color || "#101828", Number(subtitle.background_opacity ?? 0)),
    );
    element.style.setProperty(
      "--subtitle-preview-outline-shadow",
      `${-outlineWidth}px ${-outlineWidth}px 0 ${outline}, ${outlineWidth}px ${outlineWidth}px 0 ${outline}, 0 ${shadowWidth}px ${shadowWidth * 1.8}px rgba(0,0,0,.72)`,
    );
    root?.style.setProperty("--word-active", subtitle.active_color || "#FFE06A");
    root?.style.setProperty("--word-read", subtitle.read_color || "#FFFFFF");
    root?.style.setProperty("--word-scale", String(Number(subtitle.pop_scale || 112) / 100));
    $$(".caption-word", element).forEach((word) => {
      word.style.color = word.classList.contains("is-current")
        ? subtitle.active_color || "#FFE06A"
        : word.classList.contains("is-read")
          ? subtitle.read_color || "#FFFFFF"
          : subtitle.unread_color || subtitle.text_color || "#FFFFFF";
    });
  }

  function updateStylePreview() {
    const root = $("#style-preview");
    if (!root) return;
    const stylePlatform = state.platforms[0] || { name: "GoodNovel", brand_color: "#315bd8" };
    paintPlatformLogo($("#style-story-platform-mark"), stylePlatform);
    if ($("#style-story-search")) $("#style-story-search").textContent = `${stylePlatform.name || "Platform"} · Search “B56826”`;
    const subtitle = $(".preview-subtitle:not(.outro-caption-preview)", root);
    const outroSubtitle = $(".outro-caption-preview", root);
    const card = $(".code-card", root);
    const introTitle = $(".story-preview-title", root);
    const introCard = $(".story-summary-card", root);
    const introBody = $(".story-summary-card p", root);
    const introLabel = $(".story-card-label", root);
    const outro = $(".outro-style-preview", root);
    applyIntroPreviewGeometry(
      introTitle,
      introCard,
      {
        width_percent: $("#intro-width")?.value,
        position_y_percent: $("#intro-y")?.value,
        headline_font_size: $("#intro-headline-size")?.value,
      },
      {
        position_y_percent: $("#code-y")?.value,
        font_size: $("#code-size")?.value,
        padding: $("#code-padding")?.value,
      },
    );
    const videoTemplate = $("#video-template")?.value || "classic";
    const captionMode = $("#caption-mode")?.value || "semantic";
    const subtitlePreset = $("#subtitle-preset")?.value || "clear_outline";
    const subtitleAnimation = $("#subtitle-animation")?.value || "none";
    const renderMode = $("#render-mode")?.value || "speed";
    const outputFps = Number($("#output-fps")?.value || 60);
    const coverAnimation = normalizedCoverAnimation($("#cover-animation")?.value);
    const coverOutroEnabled = $("#cover-outro-enabled")?.checked !== false;
    const colorGrade = $("#color-grade")?.value || "neutral";
    const introAnimation = $("#intro-animation")?.value || "fade_rise";
    root.dataset.videoTemplate = videoTemplate;
    root.dataset.captionMode = captionMode;
    root.dataset.subtitlePreset = subtitlePreset;
    root.dataset.subtitleAnimation = subtitleAnimation;
    root.dataset.renderMode = renderMode;
    root.dataset.coverAnimation = coverAnimation;
    root.dataset.coverOutroEnabled = String(coverOutroEnabled);
    root.dataset.colorGrade = colorGrade;
    root.dataset.introAnimation = introAnimation;
    root.dataset.previewScene = state.stylePreviewScene || "intro";
    const subtitleSettings = {
      font_family: $("#subtitle-font")?.value || "Arial",
      font_size: Number($("#subtitle-size")?.value || 52),
      max_chars_per_line: Number($("#subtitle-chars")?.value || 28),
      max_lines: Number($("#subtitle-max-lines")?.value || 3),
      horizontal_margin: Number($("#subtitle-horizontal-margin").value || 180),
      bottom_margin: Number($("#subtitle-margin").value || 310),
      position_x_percent: Number($("#subtitle-position-x")?.value || 50),
      text_color: $("#subtitle-color")?.value || "#FFFFFF",
      outline_color: $("#subtitle-outline")?.value || "#101828",
      outline_width: Number($("#subtitle-outline-width")?.value || 4),
      shadow_width: Number($("#subtitle-shadow-width")?.value || 4),
      background_color: $("#subtitle-background")?.value || "#101828",
      background_opacity: Number($("#subtitle-background-opacity")?.value || 0) / 100,
      alignment: $("#subtitle-alignment")?.value || "center",
      bold: Boolean($("#subtitle-bold")?.checked),
      italic: Boolean($("#subtitle-italic")?.checked),
      unread_color: $("#subtitle-unread-color")?.value || "#FFFFFF",
      active_color: $("#subtitle-active-color")?.value || "#FFE06A",
      read_color: $("#subtitle-read-color")?.value || "#FFFFFF",
      pop_scale: Number($("#subtitle-pop-scale")?.value || 112),
    };
    const wordSync = Boolean($("#subtitle-word-sync")?.checked);
    const captionOptions = { preset: subtitlePreset, animation: subtitleAnimation, wordSync };
    applySubtitlePreviewStyles(subtitle, root, subtitleSettings, {
      ...captionOptions,
      text: "She opened the message and realized her husband had another life.",
    });
    applySubtitlePreviewStyles(outroSubtitle, root, subtitleSettings, {
      ...captionOptions,
      text: `Download ${stylePlatform.name || "the novel app"} and search B56826 to continue reading.`,
    });
    paintOutroCover($("#style-outro-cover"), previewCoverNovel(), coverAnimation);
    const coverAnimationControl = $("#cover-animation");
    if (coverAnimationControl) coverAnimationControl.disabled = !coverOutroEnabled;
    $$('[data-cover-animation-value]').forEach((button) => {
      const active = button.dataset.coverAnimationValue === coverAnimation;
      button.classList.toggle("is-active", active);
      button.disabled = !coverOutroEnabled;
      button.setAttribute("aria-disabled", String(!coverOutroEnabled));
      button.setAttribute("aria-pressed", String(active));
    });
    const cardScale = Number($("#code-size").value || 42) / 4.2;
    card.style.fontFamily = $("#code-font")?.value || "Arial";
    card.style.fontSize = `${cardScale}px`;
    card.style.fontWeight = $("#code-bold")?.checked ? "800" : "500";
    card.style.color = $("#code-color").value;
    card.style.background = colorWithOpacity(
      $("#code-background").value,
      Number($("#code-opacity").value || 92) / 100,
    );
    card.style.top = `${Number($("#code-y")?.value || 9)}%`;
    card.style.left = `${Number($("#code-x")?.value || 50)}%`;
    card.style.width = `${Number($("#code-width")?.value || 62)}%`;
    card.style.maxWidth = "none";
    card.style.padding = `${Math.max(4, Number($("#code-padding")?.value || 20) / 4.2)}px`;
    card.style.borderRadius = `${Number($("#code-radius")?.value || 12) / 4.2}px`;
    card.style.borderColor = $("#code-outline")?.value || "#FFFFFF";
    card.style.borderWidth = `${Number($("#code-outline-width")?.value || 1) / 3}px`;
    card.style.textAlign = $("#code-alignment")?.value || "center";
    card.style.transform = "translateX(-50%)";
    if (introTitle) {
      introTitle.style.fontFamily = $("#intro-font")?.value || "Arial";
      introTitle.style.fontSize = `${Number($("#intro-headline-size")?.value || 66) / 4}px`;
      introTitle.style.color = $("#intro-headline-color")?.value || "#FFE06A";
      introTitle.style.textAlign = $("#intro-alignment")?.value || "center";
    }
    if (introCard) {
      introCard.style.padding = `${Math.max(6, Number($("#intro-padding")?.value || 40) / 4.2)}px`;
      introCard.style.background = colorWithOpacity($("#intro-background")?.value || "#FFFFFF", Number($("#intro-opacity")?.value || 98) / 100);
      introCard.style.borderColor = $("#intro-border")?.value || "#FFFFFF";
      introCard.style.borderWidth = `${Number($("#intro-border-width")?.value || 2) / 3}px`;
      introCard.style.borderRadius = `${Number($("#intro-radius")?.value || 32) / 4.2}px`;
      introCard.style.boxShadow = `0 10px 28px rgba(15,27,54,${Number($("#intro-shadow-opacity")?.value || 28) / 100})`;
      introCard.style.fontFamily = $("#intro-font")?.value || "Arial";
      introCard.style.textAlign = $("#intro-alignment")?.value || "center";
    }
    if (introBody) {
      introBody.style.fontSize = `${Number($("#intro-body-size")?.value || 32) / 4}px`;
      introBody.style.color = $("#intro-body-color")?.value || "#263247";
      introBody.style.textAlign = $("#intro-alignment")?.value || "center";
      introBody.style.webkitLineClamp = String(Number($("#intro-max-lines")?.value || 5));
    }
    if (introLabel) {
      introLabel.style.fontSize = `${Number($("#intro-label-size")?.value || 24) / 4}px`;
      introLabel.style.color = $("#intro-label-color")?.value || "#315BD8";
      introLabel.style.textAlign = $("#intro-alignment")?.value || "center";
    }
    if (outro) {
      outro.style.fontFamily = $("#outro-font")?.value || "Arial";
      outro.style.left = `${Number($("#outro-x")?.value || 50)}%`;
      outro.style.top = `${Number($("#outro-y")?.value || 31)}%`;
      outro.style.width = `${Number($("#outro-width")?.value || 70)}%`;
      outro.style.height = `${Number($("#outro-height")?.value || 38)}%`;
      outro.style.padding = `${Math.max(6, Number($("#outro-padding")?.value || 42) / 4.2)}px`;
      outro.style.borderRadius = `${Number($("#outro-radius")?.value || 32) / 4.2}px`;
      outro.style.borderColor = $("#outro-border")?.value || "#D0DAE7";
      outro.style.borderWidth = `${Number($("#outro-border-width")?.value || 2) / 3}px`;
      outro.style.background = colorWithOpacity($("#outro-background")?.value || "#FFFFFF", Number($("#outro-opacity")?.value || 98) / 100);
      outro.style.textAlign = $("#outro-alignment")?.value || "center";
      const outroTitle = $("h3", outro);
      const outroBody = $("p", outro);
      const outroCode = $("strong", outro);
      if (outroTitle) { outroTitle.style.fontSize = `${Number($("#outro-title-size")?.value || 62) / 4}px`; outroTitle.style.color = $("#outro-title-color")?.value || "#17243C"; }
      if (outroBody) { outroBody.style.fontSize = `${Number($("#outro-body-size")?.value || 32) / 4}px`; outroBody.style.color = $("#outro-body-color")?.value || "#53627A"; }
      if (outroCode) { outroCode.style.fontSize = `${Number($("#outro-code-size")?.value || 42) / 4}px`; outroCode.style.color = $("#outro-code-color")?.value || "#315BD8"; }
    }
    const templateProof = $("#default-template-proof");
    if (templateProof) {
      templateProof.dataset.template = videoTemplate;
      const title = $("b", templateProof);
      const copy = $("small", templateProof);
      if (title) title.textContent = videoTemplate === "platform_story_card" ? "当前默认：平台简介卡" : "当前默认：经典模板";
      if (copy) copy.textContent = videoTemplate === "platform_story_card"
        ? "前 5.5 秒先展示标题、平台口令与故事简介。"
        : "保留顶部口令卡与醒目钩子标题。";
    }
    const strategyProof = $("#style-strategy-proof");
    if (strategyProof) {
      const captionLabels = { semantic: "语义切分", sentence: "完整句切分" };
      const presetLabels = { clear_outline: "清晰描边", cinematic_shadow: "电影阴影", clean_minimal: "极简阅读", bold_drama: "强力戏剧", reader_focus: "阅读聚焦", soft_box: "柔和底板", word_pop_sync: "逐词弹出", romance_glow: "浪漫柔光", suspense_noir: "悬疑黑金", confession_clean: "对白清透", golden_hook: "金色钩子", midnight_reader: "午夜阅读", minimal_bottom: "底部极简" };
      const animationLabels = { none: "字幕无动画", fade: "字幕淡入", soft_pop: "字幕轻弹", rise: "字幕上浮", mask_reveal: "遮罩揭示", typewriter: "逐字显现" };
      const renderLabels = { speed: "速度优先", quality: "质量优先", compatibility: "兼容模式" };
      const coverLabel = coverAnimationCatalog[coverAnimation]?.label || coverAnimationCatalog.gentle_push.label;
      strategyProof.textContent = `${captionLabels[captionMode]} · ${presetLabels[subtitlePreset]} · ${animationLabels[subtitleAnimation]} · ${outputFps} FPS · ${renderLabels[renderMode]} · ${coverOutroEnabled ? `封面${coverLabel}` : "不使用封面结尾"}`;
    }
    updateStylePresetCards();
    updateStyleRangeProofs();
    setStylePreviewScene(state.stylePreviewScene || "intro");
  }

  function stylePayload() {
    return {
      adult_mode: $('input[name="adult_mode"]:checked').value,
      narration_wpm: Number($("#setting-wpm").value),
      chapter_pause_seconds: Number($("#setting-chapter-pause").value),
      bgm_volume: Number($("#setting-bgm-volume").value) / 100,
      retention_min: 0.85,
      retention_max: 0.9,
      video_template: $("#video-template").value,
      intro_card_preset: $("#intro-card-preset").value,
      caption_mode: $("#caption-mode").value,
      subtitle_preset: $("#subtitle-preset").value,
      code_card_preset: $("#code-card-preset").value,
      outro_card_preset: $("#outro-card-preset").value,
      subtitle_animation: $("#subtitle-animation").value,
      output_fps: Number($("#output-fps").value),
      render_mode: $("#render-mode").value,
      cover_outro_enabled: $("#cover-outro-enabled")?.checked !== false,
      cover_animation: normalizedCoverAnimation($("#cover-animation").value),
      color_grade: $("#color-grade")?.value || "neutral",
      intro_animation: $("#intro-animation")?.value || "fade_rise",
      voice_by_mood: {
        suspense: $("#voice-suspense").value,
        romance: $("#voice-romance").value,
        sad: $("#voice-sad").value,
        revenge: $("#voice-revenge").value,
      },
      subtitle: {
        font_family: $("#subtitle-font").value,
        font_size: Number($("#subtitle-size").value),
        max_chars_per_line: Number($("#subtitle-chars").value),
        max_lines: Number($("#subtitle-max-lines").value),
        bottom_margin: Number($("#subtitle-margin").value),
        horizontal_margin: Number($("#subtitle-horizontal-margin").value),
        text_color: $("#subtitle-color").value.toUpperCase(),
        outline_color: $("#subtitle-outline").value.toUpperCase(),
        outline_width: Number($("#subtitle-outline-width").value),
        bold: Boolean($("#subtitle-bold").checked),
        italic: Boolean($("#subtitle-italic").checked),
        shadow_width: Number($("#subtitle-shadow-width").value),
        background_color: $("#subtitle-background").value.toUpperCase(),
        background_opacity: Number($("#subtitle-background-opacity").value) / 100,
        alignment: $("#subtitle-alignment").value,
        position_x_percent: Number($("#subtitle-position-x").value),
        word_sync_enabled: Boolean($("#subtitle-word-sync").checked),
        unread_color: $("#subtitle-unread-color").value.toUpperCase(),
        active_color: $("#subtitle-active-color").value.toUpperCase(),
        read_color: $("#subtitle-read-color").value.toUpperCase(),
        pop_scale: Number($("#subtitle-pop-scale").value),
        pop_duration_ms: Number($("#subtitle-pop-duration").value),
        pop_intensity: Number($("#subtitle-pop-intensity").value) / 100,
      },
      intro_card: {
        font_family: $("#intro-font").value,
        headline_font_size: Number($("#intro-headline-size").value),
        headline_color: $("#intro-headline-color").value.toUpperCase(),
        body_font_size: Number($("#intro-body-size").value),
        body_color: $("#intro-body-color").value.toUpperCase(),
        label_font_size: Number($("#intro-label-size").value),
        label_color: $("#intro-label-color").value.toUpperCase(),
        background_color: $("#intro-background").value.toUpperCase(),
        background_opacity: Number($("#intro-opacity").value) / 100,
        border_color: $("#intro-border").value.toUpperCase(),
        border_width: Number($("#intro-border-width").value),
        shadow_opacity: Number($("#intro-shadow-opacity").value) / 100,
        width_percent: Number($("#intro-width").value),
        position_x_percent: Number($("#intro-x").value),
        position_y_percent: Number($("#intro-y").value),
        padding: Number($("#intro-padding").value),
        radius: Number($("#intro-radius").value),
        text_alignment: $("#intro-alignment").value,
        max_lines: Number($("#intro-max-lines").value),
      },
      code_card: {
        font_family: $("#code-font").value,
        font_size: Number($("#code-size").value),
        top_margin: Number($("#code-margin").value),
        horizontal_margin: Number($("#code-horizontal-margin").value),
        opacity: Number($("#code-opacity").value) / 100,
        text_color: $("#code-color").value.toUpperCase(),
        background_color: $("#code-background").value.toUpperCase(),
        bold: Boolean($("#code-bold").checked),
        outline_color: $("#code-outline").value.toUpperCase(),
        outline_width: Number($("#code-outline-width").value),
        alignment: $("#code-alignment").value,
        position_x_percent: Number($("#code-x").value),
        position_y_percent: Number($("#code-y").value),
        width_percent: Number($("#code-width").value),
        padding: Number($("#code-padding").value),
        radius: Number($("#code-radius").value),
      },
      outro_card: {
        font_family: $("#outro-font").value,
        title_font_size: Number($("#outro-title-size").value),
        title_color: $("#outro-title-color").value.toUpperCase(),
        body_font_size: Number($("#outro-body-size").value),
        body_color: $("#outro-body-color").value.toUpperCase(),
        code_font_size: Number($("#outro-code-size").value),
        code_color: $("#outro-code-color").value.toUpperCase(),
        background_color: $("#outro-background").value.toUpperCase(),
        background_opacity: Number($("#outro-opacity").value) / 100,
        border_color: $("#outro-border").value.toUpperCase(),
        border_width: Number($("#outro-border-width").value),
        width_percent: Number($("#outro-width").value),
        height_percent: Number($("#outro-height").value),
        position_x_percent: Number($("#outro-x").value),
        position_y_percent: Number($("#outro-y").value),
        padding: Number($("#outro-padding").value),
        radius: Number($("#outro-radius").value),
        text_alignment: $("#outro-alignment").value,
      },
    };
  }

  function providerPayload() {
    const textKey = $("#text-api-key").value;
    const ttsKey = $("#tts-api-key").value;
    return {
      providers: {
        text_provider: $("#text-provider").value,
        text_model: $("#text-model").value.trim(),
        text_endpoint: $("#text-endpoint").value.trim(),
        text_api_key: textKey || (state.settings.providers.has_text_api_key ? "********" : ""),
        tts_provider: $("#tts-provider").value,
        tts_endpoint: $("#tts-endpoint").value.trim(),
        tts_api_key: ttsKey || (state.settings.providers.has_tts_api_key ? "********" : ""),
        kokoro_endpoint: $("#kokoro-endpoint").value.trim(),
        monthly_character_limit: Number($("#character-limit").value || 0),
        allow_provider_fallback: $("#provider-fallback").checked,
      },
    };
  }

  function jobsVisualSignature(jobs) {
    return JSON.stringify((jobs || []).map((job) => ({
      id: job.id,
      status: job.status,
      progress: Math.round(Number(job.progress || 0) * 1000),
      stage_label: job.stage_label || "",
      message: job.message || "",
      error_log: job.error_log || "",
      archived: Boolean(job.archived),
      preview_uri: job.preview_uri || "",
      preview_approved: Boolean(job.preview_approved),
      output_folder: job.output_folder || job.output_dir || "",
      batch_summary: job.batch_summary || null,
    })));
  }

  function queueVisualSignature(connection) {
    return JSON.stringify({
      state: connection?.state || "",
      reconnecting: Boolean(connection?.reconnecting),
      retry_in_seconds: Math.ceil(Number(connection?.retry_in_seconds || 0)),
      message: connection?.message || "",
    });
  }

  function setQueueSyncStatus(kind, message) {
    const proof = $("#queue-sync-status");
    if (!proof) return;
    proof.className = `queue-sync-status ${kind ? `is-${kind}` : ""}`;
    proof.textContent = message;
  }

  function scheduleJobPoll(delayMs = 1200, pollEpoch = state.pollEpoch) {
    if (!state.pollEnabled || pollEpoch !== state.pollEpoch) return;
    if (state.pollTimer) window.clearTimeout(state.pollTimer);
    state.pollTimer = window.setTimeout(() => {
      state.pollTimer = null;
      if (!state.pollEnabled || pollEpoch !== state.pollEpoch) return;
      void pollJobs(pollEpoch);
    }, Math.max(0, delayMs));
  }

  async function pollJobs(pollEpoch = state.pollEpoch) {
    if (!state.pollEnabled || pollEpoch !== state.pollEpoch || state.pollInFlight) return;
    state.pollInFlight = true;
    try {
      const [nextJobs, queueConnection] = await Promise.all([
        checkedCall("get_jobs"),
        checkedCall("get_queue_connection"),
      ]);
      if (!state.pollEnabled || pollEpoch !== state.pollEpoch) return;
      const normalizedQueueConnection = queueConnection && typeof queueConnection === "object"
        ? queueConnection
        : state.queueConnection;
      let recordRefreshNeeded = false;
      nextJobs.forEach((job) => {
        const before = state.previousJobStates.get(job.id);
        if (before !== job.status && terminalStatuses.has(job.status)) {
          recordRefreshNeeded = true;
        }
        if (before && before !== job.status && job.status === "completed") {
          toast(`“${job.title}”已经生成完成。`, "info");
        } else if (before && before !== job.status && job.status === "failed") {
          toast(`“${job.title}”生成失败，请查看任务详情。`, "error");
        } else if (before && before !== job.status && job.status === "awaiting_approval") {
          toast(`“${job.title}”进入了旧版等待状态；新版批次会直接生成完整视频。`, "info");
        } else if (before && before !== job.status && job.status === "interrupted") {
          toast(`“${job.title}”已在安全节点中断，可从任务卡重试。`, "error");
        }
      });
      const nextJobsSignature = jobsVisualSignature(nextJobs);
      const nextQueueSignature = queueVisualSignature(normalizedQueueConnection);
      const jobsChanged = nextJobsSignature !== state.jobVisualSignature;
      const queueChanged = nextQueueSignature !== state.queueVisualSignature;
      state.jobs = nextJobs;
      state.queueConnection = normalizedQueueConnection;
      state.previousJobStates = new Map(state.jobs.map((job) => [job.id, job.status]));
      state.jobVisualSignature = nextJobsSignature;
      state.queueVisualSignature = nextQueueSignature;
      state.pollFailureCount = 0;
      setQueueSyncStatus("ready", "任务同步正常");
      if (jobsChanged) {
        renderJobs();
        const nextDetailSignature = detailJobSignature();
        if (state.productionNovel && nextDetailSignature !== state.lastDetailJobSignature) {
          renderProductionWorkbench();
        }
        if (state.selectedNovel && nextDetailSignature !== state.lastDetailJobSignature && !$("#novel-detail-drawer")?.classList.contains("is-hidden")) {
          renderNovelDetail();
        }
        scheduleProductionRecordRefresh({ urgent: recordRefreshNeeded });
      } else if (queueChanged) {
        renderProductionState();
      }
      if (hasPollableWork()) scheduleJobPoll(1200, pollEpoch);
      else state.pollEnabled = false;
    } catch (error) {
      if (!state.pollEnabled || pollEpoch !== state.pollEpoch) return;
      state.pollFailureCount += 1;
      const retryDelay = Math.min(15000, 1200 * (2 ** Math.min(4, state.pollFailureCount - 1)));
      setQueueSyncStatus(
        state.pollFailureCount >= 4 ? "error" : "retrying",
        `任务同步中断，${Math.ceil(retryDelay / 1000)} 秒后重试`,
      );
      if (state.pollEnabled) scheduleJobPoll(retryDelay, pollEpoch);
    } finally {
      state.pollInFlight = false;
      if (state.pollEnabled && pollEpoch !== state.pollEpoch && !state.pollTimer) {
        scheduleJobPoll(0, state.pollEpoch);
      }
    }
  }

  function startPolling() {
    if (!state.pollEnabled) {
      state.pollEnabled = true;
      state.pollEpoch += 1;
    }
    if (!state.pollTimer && !state.pollInFlight) scheduleJobPoll(0, state.pollEpoch);
  }

  function stopPolling() {
    state.pollEnabled = false;
    state.pollEpoch += 1;
    if (state.pollTimer) window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
    state.pollFailureCount = 0;
    if (state.recordPollTimer) window.clearTimeout(state.recordPollTimer);
    state.recordPollTimer = null;
    state.recordPollPending = false;
    state.recordPollUrgent = false;
  }

  function bindEvents() {
    $$("[data-view-target]").forEach((item) =>
      item.addEventListener("click", () => navigate(item.dataset.viewTarget)),
    );
    document.addEventListener("click", (event) => {
      const batchStyle = event.target.closest("[data-open-batch-style-studio]");
      if (batchStyle) {
        openBatchStyleStudio();
        return;
      }
      const resource = event.target.closest("[data-resource-view]");
      if (resource) {
        navigate(resource.dataset.resourceView);
        return;
      }
      const action = event.target.closest("[data-open-view]");
      if (action) {
        if (action.dataset.openView === "styles") openGlobalStyleStudio();
        else navigate(action.dataset.openView);
        if (action.dataset.scrollTarget) {
          window.requestAnimationFrame(() => {
            document.querySelector(action.dataset.scrollTarget)?.scrollIntoView({ behavior: "smooth", block: "start" });
          });
        }
      }
    });
    document.addEventListener("click", async (event) => {
      const productionLocalTab = event.target.closest("[data-production-local-tab]");
      if (productionLocalTab) {
        setProductionLocalTab(productionLocalTab.dataset.productionLocalTab, { focus: true });
        return;
      }
      const openProductionPreviewDrawer = event.target.closest("[data-open-production-preview-drawer]");
      if (openProductionPreviewDrawer) {
        setProductionPreviewDrawerOpen(true);
        return;
      }
      const closeProductionPreviewDrawer = event.target.closest("[data-close-production-preview-drawer]");
      if (closeProductionPreviewDrawer) {
        setProductionPreviewDrawerOpen(false);
        return;
      }
      const productionSectionToggle = event.target.closest("[data-toggle-production-section]");
      if (productionSectionToggle) {
        const section = String(productionSectionToggle.dataset.toggleProductionSection || "");
        setProductionSectionExpanded(section, productionSectionToggle.getAttribute("aria-expanded") !== "true");
        return;
      }
      const batchToggle = event.target.closest("[data-toggle-job-batch]");
      if (batchToggle) {
        if (!(state.jobBatchDisclosure instanceof Map)) state.jobBatchDisclosure = new Map();
        const disclosureKey = String(batchToggle.dataset.toggleJobBatch || "");
        const isOpen = batchToggle.getAttribute("aria-expanded") === "true";
        const nextOpen = !isOpen;
        state.jobBatchDisclosure.set(disclosureKey, nextOpen);
        batchToggle.setAttribute("aria-expanded", String(nextOpen));
        const body = batchToggle.getAttribute("aria-controls")
          ? document.getElementById(batchToggle.getAttribute("aria-controls"))
          : null;
        if (body) body.hidden = !nextOpen;
        batchToggle.closest(".job-batch")?.classList.toggle("is-open", nextOpen);
        return;
      }
      const batchRecords = event.target.closest("[data-open-job-batch-records]");
      if (batchRecords) {
        state.recordStatusFilter = "";
        state.recordNovelFilter = "";
        state.recordBatchFilter = String(batchRecords.dataset.openJobBatchRecords || "");
        state.recordMemberFilter = "";
        state.recordDeviceFilter = "";
        state.recordDateFrom = "";
        state.recordDateTo = "";
        state.recordTrashFilter = false;
        navigate("records");
        await loadProductionRecordGroups();
        return;
      }
      const refreshDevices = event.target.closest("[data-refresh-managed-devices]");
      if (refreshDevices) {
        await loadManagedDeviceFleet();
        return;
      }
      const managedDeviceAction = event.target.closest("[data-managed-device-action]");
      if (managedDeviceAction) {
        try {
          if (managedDeviceAction.dataset.managedDeviceAction === "rename") {
            await openManagedDeviceDialog(managedDeviceAction.dataset.managedDeviceId);
          } else if (managedDeviceAction.dataset.managedDeviceAction === "toggle") {
            await toggleManagedDevice(managedDeviceAction.dataset.managedDeviceId, managedDeviceAction);
          } else if (managedDeviceAction.dataset.managedDeviceAction === "review") {
            const reviewed = await checkedCall("acknowledge_managed_device", managedDeviceAction.dataset.managedDeviceId);
            const index = state.managedDevices.findIndex((item) => item.id === reviewed.id);
            if (index >= 0) state.managedDevices[index] = reviewed;
            renderManagedDeviceWorkspace();
            toast("已确认这台新电脑的首次登录。", "info");
          }
        } catch (error) {
          toast(error.message, "error");
        }
        return;
      }
      const userButton = event.target.closest("[data-software-user-id]");
      if (userButton) {
        await selectSoftwareUser(userButton.dataset.softwareUserId);
        return;
      }
      const publishingAccount = event.target.closest("[data-publishing-account-id]");
      if (publishingAccount) {
        editPublishingAccount(publishingAccount.dataset.publishingAccountId);
        return;
      }
      const createPublishingAccount = event.target.closest("[data-create-publishing-account]");
      if (createPublishingAccount) {
        resetPublishingAccountEditor();
        $("#publishing-account-form [name='platform_id']")?.focus();
        return;
      }
      const clearPublishingFilter = event.target.closest("[data-clear-publishing-filter]");
      if (clearPublishingFilter) {
        state.publishingPlatformFilter = "";
        $("#publishing-platform-filter").value = "";
        renderPublishingAccounts();
        return;
      }
      const openProductionPicker = event.target.closest("[data-open-production-picker]");
      if (openProductionPicker) {
        enterProductionNovelPicker();
        return;
      }
      const cancelProductionPicker = event.target.closest("[data-cancel-production-picker]");
      if (cancelProductionPicker) {
        leaveProductionNovelPicker();
        return;
      }
      const chooseProductionNovel = event.target.closest("[data-select-production-novel]");
      if (chooseProductionNovel) {
        try {
          await withBusyButton(chooseProductionNovel, "正在读取正文并判断类型…", async () => {
            await selectProductionNovel(chooseProductionNovel.dataset.selectProductionNovel);
            state.librarySelectionMode = "";
            navigate("queue");
            $("#production-workbench-title")?.scrollIntoView({ behavior: "smooth", block: "start" });
            window.setTimeout(() => $("[data-open-production-picker]")?.focus(), 80);
          });
        } catch (error) {
          toast(error.message, "error");
        }
        return;
      }
      const startProduction = event.target.closest("[data-start-production-from-novel], [data-produce-novel]");
      if (startProduction) {
        const novelId = startProduction.dataset.produceNovel || state.selectedNovelId;
        closeNovelDetail();
        navigate("queue");
        try {
          await selectProductionNovel(novelId);
          $("#production-workbench-title")?.scrollIntoView({ behavior: "smooth", block: "start" });
        } catch (error) {
          toast(error.message, "error");
        }
        return;
      }
      const openProductionProfile = event.target.closest("[data-open-production-novel-profile]");
      if (openProductionProfile) {
        navigate("library");
        await openNovelDetail(openProductionProfile.dataset.openProductionNovelProfile, openProductionProfile);
        return;
      }
      const openImport = event.target.closest("[data-open-import]");
      if (openImport) {
        openNovelImport();
        return;
      }
      const clearFilters = event.target.closest("[data-clear-library-filters]");
      if (clearFilters) {
        state.libraryQuery = "";
        state.libraryPlatformFilter = "";
        state.libraryLanguageFilter = "";
        $("#library-search").value = "";
        $("#library-platform-filter").value = "";
        renderNovelLibrary();
        return;
      }
      const retryLibrary = event.target.closest("[data-retry-library]");
      if (retryLibrary) {
        await withBusyButton(retryLibrary, "正在读取…", async () => {
          await loadLibraryBootstrap();
        });
        return;
      }
      const languageFilter = event.target.closest("[data-language-filter]");
      if (languageFilter) {
        state.libraryLanguageFilter = languageFilter.dataset.languageFilter || "";
        renderNovelLibrary();
        return;
      }
      const openNovel = event.target.closest("[data-open-novel]");
      if (openNovel) {
        await openNovelDetail(openNovel.dataset.openNovel, openNovel);
        return;
      }
      const openRecordNovel = event.target.closest("[data-open-record-novel]");
      if (openRecordNovel) {
        navigate("library");
        await openNovelDetail(openRecordNovel.dataset.openRecordNovel, openRecordNovel);
        return;
      }
      const viewRecordArtifacts = event.target.closest("[data-view-record-artifacts]");
      if (viewRecordArtifacts) {
        await openRecordArtifacts(viewRecordArtifacts.dataset.viewRecordArtifacts, viewRecordArtifacts);
        return;
      }
      const selectRecord = event.target.closest("[data-select-record]");
      if (selectRecord) {
        const recordId = String(selectRecord.dataset.selectRecord || "");
        if (selectRecord.checked) state.selectedRecordIds.add(recordId);
        else state.selectedRecordIds.delete(recordId);
        updateRecordBulkBar(groupedProductionTasks());
        return;
      }
      const episodeSelection = event.target.closest("[data-episode-selection]");
      if (episodeSelection) {
        const selectionRoot = productionRoot();
        $$('[data-episode-id]', selectionRoot).forEach((input) => {
          input.checked = episodeSelection.dataset.episodeSelection === "all";
        });
        syncProductionDraftFromControls({ render: true });
        return;
      }
      const saveMetadata = event.target.closest("[data-save-novel-metadata]");
      if (saveMetadata) {
        await saveNovelMetadata(saveMetadata);
        return;
      }
      const redetectLanguage = event.target.closest("[data-redetect-novel-language]");
      if (redetectLanguage) {
        await redetectNovelLanguage(redetectLanguage);
        return;
      }
      const chooseCover = event.target.closest("[data-choose-novel-cover]");
      if (chooseCover) {
        await chooseNovelCover(chooseCover);
        return;
      }
      const importSynopsis = event.target.closest("[data-import-synopsis]");
      if (importSynopsis) {
        await importNovelSynopsis(importSynopsis);
        return;
      }
      const updateManuscript = event.target.closest("[data-update-manuscript]");
      if (updateManuscript) {
        openNovelImport(state.selectedNovel);
        return;
      }
      const saveBinding = event.target.closest("[data-save-binding]");
      if (saveBinding) {
        await saveNovelBinding(saveBinding);
        return;
      }
      const addCode = event.target.closest("[data-add-promo-code]");
      if (addCode) {
        await addPromoCode(addCode);
        return;
      }
      const toggleCode = event.target.closest("[data-toggle-code]");
      if (toggleCode) {
        event.preventDefault();
        await togglePromoCode(toggleCode);
        return;
      }
      const generateVoices = event.target.closest("[data-generate-voices]");
      if (generateVoices) {
        await generateVoiceCandidates(generateVoices);
        return;
      }
      const generateIntroCopy = event.target.closest("[data-generate-intro-copy]");
      if (generateIntroCopy) {
        await generateIntroCardCopy(generateIntroCopy);
        return;
      }
      const applyCompletePreset = event.target.closest("[data-apply-production-preset]");
      if (applyCompletePreset) {
        try {
          applyProductionPreset();
        } catch (error) {
          toast(error.message, "error");
        }
        return;
      }
      const saveCompletePreset = event.target.closest("[data-save-production-preset]");
      if (saveCompletePreset) {
        try {
          await saveProductionPreset(saveCompletePreset);
        } catch (error) {
          toast(error.message, "error");
        }
        return;
      }
      const saveCompletePresetAs = event.target.closest("[data-save-production-preset-as]");
      if (saveCompletePresetAs) {
        try {
          await saveProductionPreset(saveCompletePresetAs, { asNew: true });
        } catch (error) {
          toast(error.message, "error");
        }
        return;
      }
      const deleteCompletePreset = event.target.closest("[data-delete-production-preset]");
      if (deleteCompletePreset) {
        try {
          await deleteProductionPreset(deleteCompletePreset);
        } catch (error) {
          toast(error.message, "error");
        }
        return;
      }
      const reclassifyStory = event.target.closest("[data-reclassify-story]");
      if (reclassifyStory) {
        await reclassifyProductionNovel(reclassifyStory);
        return;
      }
      const stopWpmPreview = event.target.closest("[data-stop-wpm-preview]");
      if (stopWpmPreview) {
        stopProductionWpmPreview();
        return;
      }
      const productionAudioFile = event.target.closest("[data-choose-production-file]");
      if (productionAudioFile) {
        await chooseProductionAudioFile(productionAudioFile);
        return;
      }
      const previewVoice = event.target.closest("[data-preview-voice-index]");
      if (previewVoice) {
        playVoiceCandidate(previewVoice);
        return;
      }
      const draftFolder = event.target.closest("[data-draft-folder]");
      if (draftFolder) {
        await chooseDraftFolder(draftFolder);
        return;
      }
      const previewScene = event.target.closest("[data-production-preview-scene]");
      if (previewScene) {
        setProductionPreviewScene(previewScene.dataset.productionPreviewScene);
        return;
      }
      const queueDraft = event.target.closest("[data-queue-production-draft]");
      if (queueDraft) {
        await queueProductionDraft(queueDraft);
        return;
      }
      const nextProductionBatch = event.target.closest("[data-start-next-production-batch]");
      if (nextProductionBatch && state.productionNovel) {
        const currentDraft = structuredClone(activeDraft(state.productionNovel));
        state.lastQueuedBatch = {
          novelId: state.productionNovel.id,
          draftId: String(currentDraft.id || ""),
          totalVideos: jobsForDraft(state.productionNovel, currentDraft).length,
          queuedAt: new Date().toISOString(),
        };
        beginNextProductionBatch(state.productionNovel, currentDraft);
        toast("已打开新批次；原任务继续生成，上次选项已恢复，请按需调整本批分集。", "info");
        return;
      }
      const retry = event.target.closest("[data-retry-job]");
      if (retry) {
        await retryJob(retry);
        return;
      }
      const archiveBatchButton = event.target.closest("[data-archive-batch]");
      if (archiveBatchButton) {
        await archiveBatch(archiveBatchButton);
        return;
      }
      const restoreBatchButton = event.target.closest("[data-restore-batch]");
      if (restoreBatchButton) {
        await restoreBatch(restoreBatchButton);
        return;
      }
      const archive = event.target.closest("[data-archive-job]");
      if (archive) {
        await archiveJob(archive);
        return;
      }
      const restore = event.target.closest("[data-restore-job]");
      if (restore) {
        await restoreJob(restore);
        return;
      }
      const loadArchived = event.target.closest("[data-load-archived-jobs]");
      if (loadArchived) {
        try {
          await loadArchivedJobs();
        } catch (error) {
          toast(error.message, "error");
        }
        return;
      }
      const jobView = event.target.closest("[data-job-view]");
      if (jobView) {
        state.jobArchiveView = jobView.dataset.jobView === "archived" ? "archived" : "active";
        if (state.jobArchiveView === "archived" && !state.archivedJobsLoaded) {
          try {
            await loadArchivedJobs({ reset: true });
          } catch (error) {
            toast(error.message, "error");
          }
        } else {
          renderJobs();
        }
        return;
      }
      const saveDraft = event.target.closest("[data-save-production-draft]");
      if (saveDraft) {
        await saveProductionDraft(saveDraft);
        return;
      }
      const output = event.target.closest('#record-list [data-output-folder]');
      if (output) {
        event.preventDefault();
        event.stopPropagation();
        try {
          await checkedCall("open_output_folder", output.dataset.outputFolder);
        } catch (error) {
          toast(error.message, "error");
        }
      }
    });
    document.addEventListener("change", async (event) => {
      if (event.target.matches("#production-recipe-select")) {
        state.selectedProductionPresetId = String(event.target.value || "");
        renderProductionWorkbench();
        return;
      }
      if (event.target.matches("[data-managed-device-select]")) {
        const deviceId = event.target.dataset.managedDeviceSelect;
        if (event.target.checked) state.managedDeviceSelection.add(deviceId);
        else state.managedDeviceSelection.delete(deviceId);
        renderManagedDeviceWorkspace();
        return;
      }
      if (event.target.matches('input[name="managed-config-target"]')) {
        renderManagedDeviceWorkspace();
        return;
      }
      if (event.target.matches('input[name="production-output-mode"]') && state.productionNovel) {
        markProductionRecipeDirty();
        syncProductionDraftFromControls();
        renderProductionWorkbench();
        return;
      }
      if (event.target.matches('input[name="production-bgm-mode"]') && state.productionNovel) {
        markProductionRecipeDirty();
        syncProductionDraftFromControls();
        renderProductionWorkbench();
        return;
      }
      if (event.target.matches('input[name="production-wpm-preset"]') && state.productionNovel) {
        updateProductionCustomChoiceState();
        markProductionRecipeDirty();
        syncProductionDraftFromControls();
        updateProductionPreview();
        scheduleProductionWpmPreview();
        return;
      }
      if (event.target.matches('input[name="production-video-speed-preset"]') && state.productionNovel) {
        updateProductionCustomChoiceState();
        state.productionPreviewScene = "intro";
        markProductionRecipeDirty();
        syncProductionDraftFromControls();
        updateProductionPreview();
        return;
      }
      if (event.target.matches("#production-platform-select")) {
        const novel = state.productionNovel;
        const platformId = String(event.target.value || "");
        const remembered = novel ? rememberedProductionContext(novel.id) : null;
        const rememberedCode = String(remembered?.novel?.promo_codes?.[platformId] || "");
        const rememberedAccount = String(remembered?.preferences?.publishing_accounts?.[platformId] || "");
        syncProductionDraftFromControls();
        if (novel) {
          const draft = activeDraft(novel);
          const binding = bindingFor(novel, platformId);
          const validCodes = (binding?.codes || []).filter((item) => item.active);
          const validAccounts = state.publishingAccounts.filter((item) => (
            item.active !== false && item.platform_id === platformId
          ));
          draft.platform_id = platformId;
          draft.promo_code_id = validCodes.some((item) => item.id === rememberedCode) ? rememberedCode : "";
          draft.publishing_account_id = validAccounts.some((item) => item.id === rememberedAccount)
            ? rememberedAccount
            : "";
          persistProductionPreferences(novel, draft);
        }
        renderProductionWorkbench();
        return;
      }
      if (event.target.matches("#production-local-tts-provider") && state.productionNovel) {
        const select = event.target;
        select.disabled = true;
        try {
          const applied = await switchFreeLocalTtsProvider(select.value);
          renderProductionWorkbench();
          toast(`当前电脑已切换为 ${ttsProviderLabel(applied)}，请重新生成女声候选。`, "info");
        } catch (error) {
          renderProductionWorkbench();
          toast(error.message || "切换本机配音服务失败。", "error");
        }
        return;
      }
      if (event.target.matches("#voice-candidate-mood") && state.productionNovel) {
        const draft = activeDraft(state.productionNovel);
        const previousMood = draft.story_mood;
        markProductionRecipeDirty();
        syncProductionDraftFromControls();
        if (previousMood !== draft.story_mood && state.productionNovel.voice_candidates?.length) {
          state.productionNovel.voice_candidates = [];
          draft.voice = { provider: "", voice_id: "", label: "", profile: "" };
          renderProductionWorkbench();
          toast("故事类型已调整，请按新类型重新生成女声候选。", "info");
        } else {
          updateProductionPreview();
        }
        return;
      }
      if (event.target.closest("#production-workbench-content") && event.target.matches('[data-episode-id], input[name="production-voice"], input[name="production-output-mode"], #production-cover-outro-enabled, select, input[type="color"]')) {
        const previewScene = productionPreviewSceneForControl(event.target);
        if (previewScene) state.productionPreviewScene = previewScene;
        markProductionRecipeDirty();
        resetProductionStyleFromPreset(event.target);
        syncProductionDraftFromControls({ render: event.target.matches('input[name="production-voice"], #production-video-template') });
        updateProductionPreview();
        return;
      }
    });
    document.addEventListener("input", (event) => {
      if (!event.target.closest("#production-workbench-content")) return;
      if (event.target.matches("#production-wpm-custom") && state.productionNovel) {
        markProductionRecipeDirty();
        syncProductionDraftFromControls();
        updateProductionPreview();
        scheduleProductionWpmPreview();
        return;
      }
      if (event.target.matches("#production-video-speed-custom") && state.productionNovel) {
        state.productionPreviewScene = "intro";
        markProductionRecipeDirty();
        syncProductionDraftFromControls();
        updateProductionPreview();
        return;
      }
      if (event.target.matches('input[type="range"], input[type="number"], [data-draft-path], #production-source-narration-audio, #production-bgm-file')) {
        const previewScene = productionPreviewSceneForControl(event.target);
        if (previewScene) state.productionPreviewScene = previewScene;
        markProductionRecipeDirty();
        syncProductionDraftFromControls();
        updateProductionPreview();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if ($("#production-preview-drawer")?.classList.contains("is-drawer-open")) {
        setProductionPreviewDrawerOpen(false);
        return;
      }
      if (!$("#novel-detail-drawer")?.classList.contains("is-hidden")) closeNovelDetail();
    });
    $("#open-novel-import").addEventListener("click", () => openNovelImport());
    $("#close-novel-import").addEventListener("click", closeNovelImport);
    $("#cancel-novel-import").addEventListener("click", closeNovelImport);
    $$('[data-close-record-artifacts]').forEach((button) => button.addEventListener("click", closeRecordArtifactDialog));
    $("#record-artifact-dialog").addEventListener("cancel", (event) => {
      event.preventDefault();
      closeRecordArtifactDialog();
    });
    $("#record-artifact-dialog").addEventListener("click", (event) => {
      if (event.target === event.currentTarget) closeRecordArtifactDialog();
    });
    $("#new-software-user").addEventListener("click", resetSoftwareUserEditor);
    $("#software-user-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter || $("#save-software-user");
      try {
        await saveSoftwareUser(event.currentTarget, button);
      } catch (error) {
        $("#software-user-status").textContent = error.message;
        toast(error.message, "error");
      }
    });
    $("#delete-software-user")?.addEventListener("click", async (event) => {
      try {
        await deleteSelectedSoftwareUser(event.currentTarget);
      } catch (error) {
        $("#software-user-status").textContent = error.message;
        toast(error.message, "error");
      }
    });
    $$('[name="role"]', $("#software-user-form")).forEach((input) => input.addEventListener("change", (event) => {
      const form = event.target.form;
      syncSoftwareRoleCards(form);
      $("#software-user-status").textContent = softwareRoleCatalog[event.target.value]?.summary || softwareRoleCatalog.producer.summary;
    }));
    $("#software-user-permissions").addEventListener("change", (event) => {
      if (event.target.matches("[data-user-permission]")) updatePermissionOverrideCount();
    });
    $("#reset-permission-overrides").addEventListener("click", () => {
      $$('[data-user-permission]', $("#software-user-form")).forEach((select) => { select.value = "inherit"; });
      updatePermissionOverrideCount();
      $("#software-user-status").textContent = "保存后将全部恢复为账号类型默认权限。";
    });
    $("#close-novel-detail").addEventListener("click", closeNovelDetail);
    $("#novel-drawer-scrim").addEventListener("click", closeNovelDetail);
    $$('[data-import-source]').forEach((button) => {
      button.addEventListener("click", () => chooseNovelImportSource(button.dataset.importSource));
    });
    $("#novel-import-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter || $("#submit-novel-import");
      try {
        await submitNovelImport(event.currentTarget, button);
      } catch (error) {
        $("#import-dialog-status").textContent = error.message;
        toast(error.message, "error");
      }
    });
    $("#library-search").addEventListener("input", (event) => {
      state.libraryQuery = event.target.value;
      renderNovelLibrary();
    });
    $("#library-platform-filter").addEventListener("change", (event) => {
      state.libraryPlatformFilter = event.target.value;
      renderNovelLibrary();
    });
    $$('[data-library-layout]').forEach((button) => {
      button.addEventListener("click", () => {
        state.libraryLayout = button.dataset.libraryLayout;
        renderNovelLibrary();
      });
    });
    $("#record-status-filter").addEventListener("change", async (event) => {
      state.recordStatusFilter = event.target.value;
      state.selectedRecordIds.clear();
      await loadProductionRecordGroups();
    });
    const recordFilterBindings = [
      ["#record-novel-filter", "recordNovelFilter"],
      ["#record-batch-filter", "recordBatchFilter"],
      ["#record-member-filter", "recordMemberFilter"],
      ["#record-device-filter", "recordDeviceFilter"],
      ["#record-date-from", "recordDateFrom"],
      ["#record-date-to", "recordDateTo"],
    ];
    recordFilterBindings.forEach(([selector, key]) => {
      $(selector)?.addEventListener("change", async (event) => {
        state[key] = event.target.value;
        state.selectedRecordIds.clear();
        await loadProductionRecordGroups();
      });
    });
    $("#record-trash-filter")?.addEventListener("change", async (event) => {
      state.recordTrashFilter = Boolean(event.target.checked);
      state.selectedRecordIds.clear();
      await loadProductionRecordGroups();
    });
    $("#record-select-all")?.addEventListener("change", (event) => {
      state.selectedRecordIds = event.target.checked
        ? new Set(groupedProductionTasks().map((task) => String(task.id)))
        : new Set();
      renderRecords();
    });
    $("#record-retry-selected")?.addEventListener("click", (event) => void runSelectedRecordAction("retry", event.currentTarget));
    $("#record-cancel-selected")?.addEventListener("click", (event) => void runSelectedRecordAction("cancel", event.currentTarget));
    $("#record-trash-selected")?.addEventListener("click", (event) => void runSelectedRecordAction("trash", event.currentTarget));
    $("#record-restore-selected")?.addEventListener("click", (event) => void runSelectedRecordAction("restore", event.currentTarget));
    $("#record-delete-selected")?.addEventListener("click", (event) => void runSelectedRecordAction("delete", event.currentTarget));
    $("#safe-zone-toggle").addEventListener("change", (event) => {
      $("#safe-zone").classList.toggle("is-hidden", !event.target.checked);
    });

    $("#start-queue").addEventListener("click", async (event) => {
      try {
        await withBusyButton(event.currentTarget, "正在启动…", async () => {
          state.jobs = await checkedCall("start_queue");
          state.previousJobStates = new Map(state.jobs.map((job) => [job.id, job.status]));
          renderJobs();
          startPolling();
          toast("制作队列已开始。窗口可以保持在后台运行。");
        });
      } catch (error) {
        toast(error.message, "error");
      } finally {
        renderProductionState();
      }
    });
    $("#cancel-queue").addEventListener("click", async (event) => {
      try {
        await withBusyButton(event.currentTarget, "正在停止…", async () => {
          state.jobs = await checkedCall("cancel_queue");
          renderJobs();
          toast("队列将在当前安全节点停止。", "info");
        });
      } catch (error) {
        toast(error.message, "error");
      } finally {
        renderProductionState();
      }
    });
    $("#clear-finished").addEventListener("click", async (event) => {
      try {
        await withBusyButton(event.currentTarget, "正在归档…", async () => {
          const result = await checkedCall("archive_finished_jobs");
          await applyJobMutationResult(result);
          toast("已结束任务已归档，成片和日志仍会保留。", "info");
        });
      } catch (error) {
        toast(error.message, "error");
      } finally {
        renderProductionState();
      }
    });

    $("#new-platform").addEventListener("click", resetPlatformEditor);
    $("#choose-platform-logo").addEventListener("click", (event) => choosePlatformLogo(event.currentTarget));
    $("#clear-platform-logo").addEventListener("click", clearPlatformLogo);
    $("#new-publishing-account").addEventListener("click", resetPublishingAccountEditor);
    $("#publishing-platform-filter").addEventListener("change", (event) => {
      state.publishingPlatformFilter = event.target.value;
      renderPublishingAccounts();
    });
    $("#publishing-account-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter || $('button[type="submit"]', event.currentTarget);
      const payload = formPayload(event.currentTarget);
      payload.active = event.currentTarget.elements.active.checked;
      const currentAccount = state.publishingAccounts.find((item) => item.id === payload.id);
      if (currentAccount?.row_version) payload.expected_version = Number(currentAccount.row_version);
      try {
        await withBusyButton(button, "正在保存…", async () => {
          const account = await checkedCall("save_publishing_account", payload);
          const index = state.publishingAccounts.findIndex((item) => item.id === account.id);
          if (index >= 0) state.publishingAccounts[index] = account;
          else state.publishingAccounts.push(account);
          state.selectedPublishingAccountId = account.id;
          renderPublishingAccounts();
          editPublishingAccount(account.id);
          toast(`发布账号“${account.name}”已保存。`);
        });
      } catch (error) {
        toast(error.message, "error");
      }
    });
    ["name", "search_template"].forEach((name) => {
      $(`#platform-form [name="${name}"]`).addEventListener("input", () => {
        updatePlatformTemplatePreview();
        if (name === "name") updatePlatformBrandPreview();
      });
    });
    $('#platform-form [name="brand_color"]').addEventListener("input", updatePlatformBrandPreview);
    $("#platform-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter || $('button[type="submit"]', event.currentTarget);
      try {
        await withBusyButton(button, "正在保存…", async () => {
          const platform = await checkedCall("save_platform", formPayload(event.currentTarget));
          const index = state.platforms.findIndex((item) => item.id === platform.id);
          if (index >= 0) state.platforms[index] = platform;
          else state.platforms.push(platform);
          state.selectedPlatformId = platform.id;
          renderPlatforms();
          renderPlatformOptions();
          editPlatform(platform.id);
          toast(`平台“${platform.name}”已保存。`);
        });
      } catch (error) {
        toast(error.message, "error");
      }
    });
    $("#delete-platform").addEventListener("click", async () => {
      if (!state.selectedPlatformId || !window.confirm("确定删除这个平台档案？")) return;
      try {
        await checkedCall("delete_platform", state.selectedPlatformId);
        state.platforms = state.platforms.filter((item) => item.id !== state.selectedPlatformId);
        resetPlatformEditor();
        renderPlatformOptions();
        toast("平台档案已删除。", "info");
      } catch (error) {
        toast(error.message, "error");
      }
    });

    $$('[data-view="styles"] input, [data-view="styles"] select').forEach((control) =>
      control.addEventListener("input", () => {
        if (control.id === "cover-animation") {
          setStyleEditorPanel("outro");
          setStylePreviewScene("outro");
        }
        updateStylePreview();
        updateStyleRangeProofs();
      }),
    );
    $$('[data-style-panel]').forEach((button) => {
      button.addEventListener("click", () => {
        const category = button.dataset.stylePanel || "intro";
        setStyleEditorPanel(category);
        setStylePreviewScene(category);
        updateStylePreview();
      });
    });
    $$('[data-style-preset-target]').forEach((button) => {
      button.addEventListener("click", () => {
        const controlId = button.dataset.stylePresetTarget || "";
        applyStylePreset(styleCategoryForControl(controlId), button.dataset.stylePresetValue || "");
      });
    });
    $$('[data-cover-animation-value]').forEach((button) => {
      button.addEventListener("click", () => {
        const value = normalizedCoverAnimation(button.dataset.coverAnimationValue);
        setStyleControlValue("cover-animation", value);
        setStyleEditorPanel("outro");
        setStylePreviewScene("outro");
        updateStylePreview();
      });
    });
    $$('[data-style-preview-scene]').forEach((button) => {
      button.addEventListener("click", () => setStylePreviewScene(button.dataset.stylePreviewScene || "intro"));
    });
    $("#toggle-style-safe-zone")?.addEventListener("click", (event) => {
      const safeZone = $(".safe-zone", $("#style-preview"));
      if (!safeZone) return;
      const hidden = safeZone.classList.toggle("is-hidden");
      event.currentTarget.textContent = hidden ? "显示安全区" : "隐藏安全区";
    });
    $("#save-custom-style-preset")?.addEventListener("click", async (event) => {
      try {
        await withBusyButton(event.currentTarget, "正在保存…", () => savePersonalStylePreset());
      } catch (error) {
        toast(error.message, "error");
      }
    });
    $("#custom-style-presets")?.addEventListener("change", (event) => {
      const preset = productionPresetManagementItems().find((item) => item.id === event.target.value);
      const remove = $("#delete-custom-style-preset");
      if (remove) {
        remove.disabled = !preset?.deletable;
        remove.textContent = preset?.owned_by_current_user ? "删除我的方案" : preset?.deletable ? "管理员删除" : "删除";
      }
      if (preset) applyCustomStylePreset(preset.id);
    });
    $("#delete-custom-style-preset")?.addEventListener("click", async (event) => {
      const select = $("#custom-style-presets");
      const preset = productionPresetManagementItems().find((item) => item.id === select?.value);
      if (!preset?.deletable || !window.confirm(`删除制作方案“${preset.name}”？`)) return;
      try {
        await withBusyButton(event.currentTarget, "正在删除…", async () => {
          if (preset.legacy_local) {
            state.customStylePresets = state.customStylePresets.filter((item) => item.id !== preset.local_style_id);
            persistCustomStylePresets();
          } else {
            await checkedCall("delete_production_preset", preset.id);
            state.customStylePresets = state.customStylePresets.filter((item) => item.production_preset_id !== preset.id);
            persistCustomStylePresets();
            await refreshProductionPresets();
          }
          if (state.selectedProductionPresetId === preset.id) state.selectedProductionPresetId = "";
          renderCustomStylePresets();
          renderProductionWorkbench();
          toast("制作方案已删除。", "info");
        });
      } catch (error) {
        toast(error.message, "error");
      }
    });
    $("#style-back")?.addEventListener("click", () => {
      const destination = state.styleEditingScope === "batch" ? "queue" : "settings";
      state.styleEditingScope = "global";
      navigate(destination);
    });
    $("#setting-bgm-volume").addEventListener("input", (event) => {
      $("#setting-bgm-volume-value").textContent = `${event.target.value}%`;
    });
    $("#setting-wpm").addEventListener("input", updateSpeedPresetState);
    $$("[data-wpm]").forEach((button) => {
      button.addEventListener("click", () => {
        $("#setting-wpm").value = button.dataset.wpm;
        $("#setting-wpm").dispatchEvent(new Event("input", { bubbles: true }));
        updateSpeedPresetState();
      });
    });
    $("#save-style").addEventListener("click", async (event) => {
      if (state.styleEditingScope === "batch") {
        applyVisualStyleToCurrentBatch();
        return;
      }
      if (state.styleEditingScope === "personal") {
        try {
          await withBusyButton(event.currentTarget, "正在保存…", () => savePersonalStylePreset({ fallbackName: "我的默认方案", stableId: "style-personal-default" }));
        } catch (error) {
          toast(error.message, "error");
        }
        return;
      }
      try {
        await withBusyButton(event.currentTarget, "正在保存…", async () => {
          state.settings = await checkedCall("save_settings", stylePayload());
          loadSettingsIntoControls();
          toast("团队默认样式已保存；之后新建的批次会自动使用。", "info");
        });
      } catch (error) {
        toast(error.message, "error");
      }
    });
    $("#text-provider").addEventListener("change", updateProviderHints);
    $$("#tts-provider, #text-api-key, #tts-api-key, #kokoro-endpoint").forEach((control) => {
      control.addEventListener("input", renderProviderStatus);
      control.addEventListener("change", () => {
        renderProviderStatus();
        renderHealth();
      });
    });
    $("#employee-local-tts-provider")?.addEventListener("change", async (event) => {
      const select = event.currentTarget;
      select.disabled = true;
      try {
        const applied = await switchFreeLocalTtsProvider(select.value);
        toast(`这台电脑已切换为 ${ttsProviderLabel(applied)}。`, "info");
      } catch (error) {
        renderLocalMaintenanceStatus();
        toast(error.message || "切换本机配音服务失败。", "error");
      } finally {
        select.disabled = false;
      }
    });
    $("#open-local-maintenance-update")?.addEventListener("click", () => {
      const trigger = $("#open-employee-update");
      if (trigger) trigger.click();
      else toast("当前版本尚未提供独立软件更新窗口，请安装新版后重试。", "error");
    });
    $("#save-providers").addEventListener("click", async (event) => {
      try {
        await withBusyButton(event.currentTarget, "正在保存…", async () => {
          state.settings = await checkedCall("save_settings", providerPayload());
          $("#text-api-key").value = "";
          $("#tts-api-key").value = "";
          loadSettingsIntoControls();
          toast("服务连接已安全保存。", "info");
        });
      } catch (error) {
        toast(error.message, "error");
      }
    });
    $("#run-worker-self-check")?.addEventListener("click", async (event) => {
      try {
        await withBusyButton(event.currentTarget, "正在自检…", async () => {
          if (isAuthenticatedHubBrowser()) {
            const worker = state.localWorker || await connectLocalWorker({ quiet: false, force: true });
            if (worker) {
              const result = await localWorkerRpc("worker_self_check", []);
              if (!result?.ok) {
                throw new Error(result?.error || "当前制作电脑自检失败。");
              }
            }
          } else {
            const selfCheck = await checkedCall("get_local_self_check");
            state.localWorkerSelfCheck = selfCheck && typeof selfCheck === "object"
              ? { ...selfCheck }
              : null;
            if (selfCheck?.runtime) {
              state.system = {
                ...(state.system || {}),
                ffmpeg_ready: Boolean(selfCheck.runtime.ffmpeg_ready),
                encoders: [...(selfCheck.runtime.encoders || [])],
                recommended_encoder: String(selfCheck.runtime.recommended_encoder || ""),
                edge_tts_runtime_ready: Boolean(selfCheck.runtime.edge_tts_runtime_ready),
                embedded_kokoro_ready: Boolean(selfCheck.runtime.embedded_kokoro_ready),
              };
            }
          }
          renderHealth();
          toast(state.localWorkerSelfCheck?.ready === false ? "自检完成：请处理红色项目后再制作。" : "当前制作电脑自检完成。", state.localWorkerSelfCheck?.ready === false ? "error" : "info");
        });
      } catch (error) {
        toast(error.message || "当前制作电脑自检失败。", "error");
      }
    });
    $("#hub-mode").addEventListener("change", updateHubModeHelp);
    $("#refresh-managed-devices")?.addEventListener("click", async (event) => {
      try {
        await withBusyButton(event.currentTarget, "正在刷新…", () => loadManagedDeviceFleet());
      } catch (error) {
        toast(error.message, "error");
      }
    });
    $("#select-all-managed-devices")?.addEventListener("click", () => {
      const activeIds = state.managedDevices.filter((item) => item.active !== false).map((item) => item.id);
      const allSelected = activeIds.length && activeIds.every((deviceId) => state.managedDeviceSelection.has(deviceId));
      state.managedDeviceSelection = new Set(allSelected ? [] : activeIds);
      renderManagedDeviceWorkspace();
    });
    $("#managed-config-bgm")?.addEventListener("input", (event) => {
      if ($("#managed-config-bgm-value")) $("#managed-config-bgm-value").textContent = `${Number(event.target.value || 0)}%`;
    });
    $("#push-managed-device-config")?.addEventListener("click", async (event) => {
      try {
        await pushManagedDeviceConfig(event.currentTarget);
      } catch (error) {
        setManagedConfigStatus(error.message || "制作设置下发失败。", "error");
        toast(error.message, "error");
      }
    });
    $("#sync-device-config-now")?.addEventListener("click", async (event) => {
      try {
        await syncDeviceConfigNow(event.currentTarget);
      } catch (error) {
        state.deviceSyncStatus = { ...(state.deviceSyncStatus || {}), state: "offline", last_error: error.message || "同步失败。" };
        renderDeviceSyncStatus();
        toast(error.message, "error");
      }
    });
    $$('[data-close-managed-device]').forEach((button) => button.addEventListener("click", closeManagedDeviceDialog));
    $("#managed-device-dialog")?.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeManagedDeviceDialog();
    });
    $("#managed-device-dialog")?.addEventListener("click", (event) => {
      if (event.target === event.currentTarget) closeManagedDeviceDialog();
    });
    $("#managed-device-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter || $("#save-managed-device-name");
      try {
        await saveManagedDeviceName(event.currentTarget, button);
      } catch (error) {
        $("#managed-device-dialog-status").textContent = error.message;
        toast(error.message, "error");
      }
    });
    $("#check-hub-status").addEventListener("click", async (event) => {
      await checkHubStatus(event.currentTarget);
    });
    $("#connect-hub-password")?.addEventListener("click", async (event) => {
      try {
        await connectHubWithPassword(event.currentTarget);
      } catch (error) {
        setHubAccountConnectStatus(error.message || "连接失败，请检查地址、账号和密码。", "error");
        toast(error.message, "error");
      }
    });
    $("#copy-hub-endpoint").addEventListener("click", async (event) => {
      const endpoint = event.currentTarget.dataset.endpoint?.trim() || "";
      if (!endpoint) {
        toast("主电脑服务尚未启动，请重启 StoryForge 后再检查。", "error");
        return;
      }
      try {
        await navigator.clipboard.writeText(endpoint);
      } catch (_error) {
        const fallback = document.createElement("textarea");
        fallback.value = endpoint;
        fallback.setAttribute("readonly", "");
        fallback.style.position = "fixed";
        fallback.style.opacity = "0";
        document.body.append(fallback);
        fallback.select();
        document.execCommand("copy");
        fallback.remove();
      }
      toast("团队访问地址已复制。制作电脑可填入 StoryForge 客户端，浏览器可直接打开并登录。", "info");
    });
    $("#copy-hub-client-web-endpoint")?.addEventListener("click", async (event) => {
      const endpoint = event.currentTarget.dataset.endpoint?.trim() || "";
      if (!endpoint) {
        toast("本机制作网页尚未启动，请先连接主电脑并重启 StoryForge。", "error");
        return;
      }
      try {
        await navigator.clipboard.writeText(endpoint);
      } catch (_error) {
        const fallback = document.createElement("textarea");
        fallback.value = endpoint;
        fallback.setAttribute("readonly", "");
        fallback.style.position = "fixed";
        fallback.style.opacity = "0";
        document.body.append(fallback);
        fallback.select();
        document.execCommand("copy");
        fallback.remove();
      }
      toast("本机制作网址已复制，只能在当前制作电脑打开。", "info");
    });
    $("#save-hub-settings").addEventListener("click", async (event) => {
      try {
        await withBusyButton(event.currentTarget, "正在保存…", async () => {
          state.settings = await checkedCall("save_settings", hubSettingsPayload());
          loadSettingsIntoControls();
          state.hubRuntimeStatus = await checkedCall("get_hub_status");
          renderHubStatus(state.hubRuntimeStatus);
          state.updateStatus = await checkedCall("get_update_status");
          renderUpdateStatus(state.updateStatus);
          await refreshHubDeviceWorkspace({ silent: true });
          toast("Hub 设置已保存；请重启 StoryForge 让模式切换生效。", "info");
        });
      } catch (error) {
        toast(error.message, "error");
      }
    });
    $("#check-for-updates")?.addEventListener("click", async (event) => {
      await runUpdateAction(event.currentTarget, "正在检查…", "check_for_updates");
    });
    $("#download-update")?.addEventListener("click", async (event) => {
      await runUpdateAction(event.currentTarget, "正在下载…", "download_update", "更新包已下载并完成校验。");
    });
    $("#schedule-update")?.addEventListener("click", async (event) => {
      await runUpdateAction(event.currentTarget, "正在安排…", "schedule_update_on_restart", "已安排在下次重启 StoryForge 时安装。 ");
    });
    $("#cancel-scheduled-update")?.addEventListener("click", async (event) => {
      await runUpdateAction(event.currentTarget, "正在取消…", "cancel_scheduled_update", "已取消重启安装。 ");
    });
    $("#open-employee-update")?.addEventListener("click", () => {
      void openEmployeeUpdateDialog();
    });
    $$('[data-close-employee-update]').forEach((button) => button.addEventListener("click", closeEmployeeUpdateDialog));
    $("#employee-check-for-updates")?.addEventListener("click", async (event) => {
      if (hasDesktopBridge()) {
        await runUpdateAction(event.currentTarget, "正在检查…", "check_for_updates");
        return;
      }
      try {
        await withBusyButton(event.currentTarget, "正在检查…", loadBrowserUpdateStatus);
      } catch (error) {
        toast(error.message, "error");
      }
    });
    $("#employee-download-update")?.addEventListener("click", async (event) => {
      if (hasDesktopBridge()) {
        await runUpdateAction(event.currentTarget, "正在下载…", "download_update", "更新包已下载并完成校验。");
        return;
      }
      const downloadUrl = String(state.browserUpdateDownloadUrl || "");
      if (!downloadUrl.startsWith("/web/api/update/package?")) {
        toast("请先检查主电脑发布的客户端版本。", "error");
        return;
      }
      event.currentTarget.disabled = true;
      window.location.assign(downloadUrl);
      window.setTimeout(() => { event.currentTarget.disabled = false; }, 1200);
    });
    $("#employee-schedule-update")?.addEventListener("click", async (event) => {
      await runUpdateAction(event.currentTarget, "正在安排…", "schedule_update_on_restart", "已安排在正常关闭 StoryForge 后安装。");
    });
    $("#employee-cancel-scheduled-update")?.addEventListener("click", async (event) => {
      await runUpdateAction(event.currentTarget, "正在取消…", "cancel_scheduled_update", "已取消安装安排。");
    });
    $("#employee-restart-update")?.addEventListener("click", async (event) => {
      if (!window.confirm("现在关闭 StoryForge 并安装更新？安装完成后会自动重新打开。")) return;
      try {
        await withBusyButton(event.currentTarget, "正在安全退出…", async () => {
          const status = await checkedCall("restart_to_apply_update");
          renderUpdateStatus(status);
          toast(status?.exit_queued === false ? (status.message || "当前任务结束后再重启安装。") : "正在退出并安装更新，请稍候。", "info");
        });
      } catch (error) {
        toast(error.message, "error");
      }
    });
    $("#choose-update-package")?.addEventListener("click", async (event) => {
      try {
        await withBusyButton(event.currentTarget, "正在选择…", async () => {
          const packagePath = await checkedCall("choose_file", "update_package");
          if (packagePath) $("#update-package-path").value = String(packagePath);
        });
      } catch (error) {
        toast(error.message, "error");
      }
    });
    $("#publish-update")?.addEventListener("click", async (event) => {
      const packagePath = $("#update-package-path")?.value.trim() || "";
      const version = $("#update-publish-version")?.value.trim() || "";
      const releaseNotes = $("#update-release-notes")?.value.trim() || "";
      if (!packagePath || !version) {
        toast("请选择更新包并填写版本号。", "error");
        return;
      }
      try {
        await withBusyButton(event.currentTarget, "正在发布…", async () => {
          const result = await checkedCall("publish_update", packagePath, version, releaseNotes);
          renderUpdateStatus(result);
          toast(`StoryForge ${version} 已发布给团队。`, "info");
        });
      } catch (error) {
        toast(error.message, "error");
      }
    });
    $("#clear-published-update")?.addEventListener("click", async (event) => {
      if (!window.confirm("撤回当前团队更新？已经下载的电脑不会被强制删除更新包。")) return;
      await runUpdateAction(event.currentTarget, "正在撤回…", "clear_published_update", "团队更新已撤回。 ");
    });
  }

  function closeWebPasswordDialog() {
    const dialog = $("#web-password-dialog");
    $("#web-password-form")?.reset();
    if ($("#web-password-status")) $("#web-password-status").textContent = "";
    if (dialog?.open && dialog.close) dialog.close();
    else dialog?.removeAttribute("open");
  }

  function openWebPasswordDialog() {
    const dialog = $("#web-password-dialog");
    if (!dialog) return;
    if (dialog.showModal) dialog.showModal();
    else dialog.setAttribute("open", "");
    window.setTimeout(() => $("#web-current-password")?.focus(), 30);
  }

  function bindWebRuntimeEvents() {
    $("#web-login-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (webRuntime.loginInProgress) return;
      const username = $("#web-login-username")?.value.trim() || "";
      const password = $("#web-login-password")?.value || "";
      const remember = Boolean($("#web-login-remember")?.checked);
      const button = $("#web-login-submit");
      const status = $("#web-login-status");
      if (!username || !password) {
        if (status) status.textContent = "请输入成员账号和密码。";
        return;
      }
      webRuntime.loginInProgress = true;
      if (button) {
        button.disabled = true;
        button.textContent = "正在连接…";
      }
      if (status) status.textContent = "";
      try {
        const result = hasDesktopBridge()
          ? await bridge.call("desktop_login", username, password)
          : await webRequest("/web/api/session/login", {
            method: "POST",
            jsonBody: { username, password, remember },
            allowUnauthorized: true,
          });
        if (!result?.ok || !result.data?.user) throw new Error(result?.error || "账号或密码不正确。 ");
        applyWebSession(result.data);
        showWorkerAutostartNotice(result.data);
        if ($("#web-login-password")) $("#web-login-password").value = "";
        await connectLocalWorker({ quiet: true });
        await bootstrap();
        if (result.data.must_set_password || result.data.password_configured === false) {
          toast("首次登录成功。请设置新的 8 位登录密码。", "info");
          window.setTimeout(openWebPasswordDialog, 240);
        }
      } catch (error) {
        requireWebLogin(error.message || "登录失败，请重试。 ");
      } finally {
        webRuntime.loginInProgress = false;
        if (button) {
          button.disabled = false;
          button.textContent = "进入制作台";
        }
      }
    });
    $("#web-logout")?.addEventListener("click", async () => {
      try {
        if (hasDesktopBridge()) await bridge.call("desktop_logout");
        else await webRequest("/web/api/session", { method: "DELETE", allowUnauthorized: true });
      } finally {
        requireWebLogin("已经安全退出。 ");
      }
    });
    $("#web-change-password")?.addEventListener("click", openWebPasswordDialog);
    $$('[data-close-web-password]').forEach((button) => button.addEventListener("click", closeWebPasswordDialog));
    $("#web-password-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const currentPassword = $("#web-current-password")?.value || "";
      const newPassword = $("#web-new-password")?.value || "";
      const confirmation = $("#web-confirm-password")?.value || "";
      const status = $("#web-password-status");
      if (!/^[!-~]{8}$/.test(newPassword)) {
        if (status) status.textContent = "密码必须恰好8位，只能使用可见ASCII字符。";
        return;
      }
      if (newPassword !== confirmation) {
        if (status) status.textContent = "两次输入的新密码不一致。";
        return;
      }
      const button = $("#web-password-submit");
      try {
        await withBusyButton(button, "正在保存…", async () => {
          const result = await webRequest("/web/api/session/password", {
            method: "POST",
            jsonBody: { current_password: currentPassword, new_password: newPassword },
          });
          if (!result?.ok) throw new Error(result?.error || "密码设置失败。 ");
          if (state.webSession) {
            state.webSession.password_configured = true;
            state.webSession.must_set_password = false;
          }
          if ($("#web-change-password")) $("#web-change-password").textContent = "修改密码";
          closeWebPasswordDialog();
          toast("网页登录密码已更新。", "info");
        });
      } catch (error) {
        if (status) status.textContent = error.message;
      }
    });
  }

  async function bootstrap() {
    if (state.bootstrapped) return;
    state.bootstrapped = true;
    try {
      const data = await checkedCall("get_bootstrap");
      Object.assign(state, data);
      applyArchivedJobPage({
        items: Array.isArray(data.archived_jobs) ? data.archived_jobs : [],
        total: Number(data.archived_jobs_total ?? data.archived_jobs?.length ?? 0),
      });
      state.productionPresets = Array.isArray(data.production_presets?.items)
        ? data.production_presets.items
        : [];
      if (!productionPresetItems().some((item) => item.id === state.selectedProductionPresetId)) {
        state.selectedProductionPresetId = "";
      }
      state.webDefaultFolders = state.localWorker?.folders
        ? { ...state.localWorker.folders }
        : (data.web_default_folders && typeof data.web_default_folders === "object"
          ? { ...data.web_default_folders }
          : {});
      if (state.localWorker) {
        try {
          const [jobs, queueConnection] = await Promise.all([
            checkedCall("get_jobs"),
            checkedCall("get_queue_connection"),
          ]);
          state.jobs = jobs;
          state.queueConnection = queueConnection && typeof queueConnection === "object"
            ? queueConnection
            : state.queueConnection;
        } catch (_error) {
          state.jobs = [];
        }
      }
      state.hubRuntimeStatus = data.hub_status || null;
      state.deviceSyncStatus = data.device_sync || data.hub_status?.device_sync || null;
      state.updateStatus = data.update_status || null;
      state.previousJobStates = new Map(state.jobs.map((job) => [job.id, job.status]));
      renderPlatformOptions();
      renderPlatforms();
      renderJobs();
      renderHealth();
      loadSettingsIntoControls();
      if (state.hubRuntimeStatus) renderHubStatus(state.hubRuntimeStatus);
      renderDeviceSyncStatus();
      if (state.updateStatus) renderUpdateStatus(state.updateStatus);
      loadCustomStylePresets();
      updateStyleScopeUI();
      resetPlatformEditor();
      await loadLibraryBootstrap();
      navigate("queue");
      startRuntimeCapabilityRefresh();
      if (hasPollableWork()) startPolling();
    } catch (error) {
      state.bootstrapped = false;
      toast(error.message, "error");
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    bindWebRuntimeEvents();
    if (hasDesktopBridge()) window.setTimeout(initializeDesktopRuntime, 0);
    else if (isWebRuntime) window.setTimeout(initializeWebRuntime, 80);
  });
  window.addEventListener("focus", () => {
    refreshLocalRuntimeCapabilities({ render: true });
  });
  window.addEventListener("pywebviewready", () => {
    if (hasDesktopBridge()) initializeDesktopRuntime();
    else if (isWebRuntime && !state.bootstrapped) initializeWebRuntime();
  });
})();
