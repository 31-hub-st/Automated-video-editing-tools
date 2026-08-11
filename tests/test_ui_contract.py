from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StaticUiAssetContractTests(unittest.TestCase):
    def test_studio_theme_is_referenced_and_non_empty(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        theme = ROOT / "ui" / "studio-theme.css"

        self.assertIn("studio-theme.css", html)
        self.assertTrue(theme.is_file())
        self.assertGreater(theme.stat().st_size, 0)

    def test_offline_update_status_does_not_embed_a_stale_release(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        self.assertIn('current_version: ""', javascript)
        self.assertNotIn('current_version: "0.4.0-rc3"', javascript)


class StyleSettingsContractTests(unittest.TestCase):
    def test_output_fps_defaults_to_60_and_remains_selectable_per_batch(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="output-fps"', html)
        self.assertIn('<option value="60">60 FPS（推荐，更流畅）</option>', html)
        self.assertIn('<option value="30">30 FPS（生成更快）</option>', html)
        self.assertIn('id="preview-output">1080 × 1920 · 60 FPS', html)
        self.assertIn('id="production-output-fps"', javascript)
        self.assertIn("const PRODUCTION_WPM_PRESETS = [220, 240, 260, 280]", javascript)
        self.assertIn('id="production-wpm-custom" type="number" min="200" max="280"', javascript)
        self.assertIn('output_fps: Number($("#output-fps").value)', javascript)
        self.assertIn(
            'productionSettings.output_fps = Number($("#production-output-fps")',
            javascript,
        )

    def test_background_music_control_is_loaded_and_saved_as_a_ratio(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="setting-bgm-volume"', html)
        self.assertIn('min="5" max="50" step="1" value="28"', html)
        self.assertIn('assignValue("#setting-bgm-volume"', javascript)
        self.assertIn(
            'bgm_volume: Number($("#setting-bgm-volume").value) / 100',
            javascript,
        )
        self.assertIn('id="setting-bgm-volume-value"', html)

    def test_production_dashboard_has_state_driven_queue_controls(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        for element_id in (
            "queue-status-title",
            "metric-queued",
            "metric-active",
            "metric-approval",
            "metric-completed",
            "metric-failed",
            "clear-finished",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('id="web-login-remember" type="checkbox" class="is-hidden" checked', html)
        self.assertIn("登录后直接制作", html)
        self.assertIn("无需填写主机地址或电脑名称", html)
        self.assertIn("本机制作服务随 StoryForge 自动启动", html)
        self.assertIn("function renderProductionState()", javascript)
        self.assertIn('checkedCall("archive_finished_jobs")', javascript)
        self.assertIn('checkedCall("open_output_folder"', javascript)

    def test_job_tape_archives_without_deleting_history(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('data-job-view="active"', html)
        self.assertIn('data-job-view="archived"', html)
        self.assertIn('id="job-active-count"', html)
        self.assertIn('id="job-archived-count"', html)
        self.assertIn("归档只收起已结束任务，不会删除成片和错误日志", html)
        for method in (
            "archive_job",
            "restore_job",
            "archive_finished_jobs",
            "archive_batch",
            "restore_batch",
        ):
            self.assertIn(f'checkedCall("{method}"', javascript)
            self.assertIn(f'method === "{method}"', javascript)
        self.assertIn('data-archive-batch=', javascript)
        self.assertIn('data-restore-batch=', javascript)
        self.assertIn("归档整批", javascript)
        self.assertIn("恢复整批", javascript)
        self.assertIn('checkedCall("get_archived_jobs"', javascript)
        self.assertIn("state.archivedJobs", javascript)
        self.assertIn("data-load-archived-jobs", javascript)
        self.assertNotIn(
            "const archivedJobs = state.jobs.filter((job) => Boolean(job.archived))",
            javascript,
        )
        self.assertIn("terminalStatuses.has(job.status)", javascript)
        self.assertIn("Boolean(job.archived)", javascript)
        self.assertNotIn('checkedCall("clear_finished_jobs")', javascript)
        self.assertIn(".queue-view-switch", css)
        self.assertIn(".job-card.is-archived", css)

    def test_failed_jobs_do_not_present_stale_progress_as_active_work(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        self.assertIn(
            'const unsuccessfulTerminalStatuses = new Set(["failed", "cancelled", "interrupted"])',
            javascript,
        )
        self.assertIn(
            "const showProgress = !unsuccessfulTerminalStatuses.has(job.status);",
            javascript,
        )
        self.assertIn(
            "const showProgress = !unsuccessfulTerminalStatuses.has(record.status);",
            javascript,
        )
        self.assertIn('${showProgress ? `<div class="job-progress"', javascript)

    def test_job_tape_groups_videos_into_persistent_batch_dossiers(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("每批视频收在一张批次卡里", html)
        self.assertIn("0批 · 0条", html)
        self.assertIn("jobBatchDisclosure: new Map()", javascript)
        self.assertIn("function groupJobsByBatch(jobs)", javascript)
        self.assertIn("job?.batch_id", javascript)
        key_start = javascript.index("function jobBatchKey(job)")
        key_end = javascript.index("function groupJobsByBatch(jobs)", key_start)
        key_contract = javascript[key_start:key_end]
        self.assertLess(
            key_contract.index("job?.batch_id"),
            key_contract.index("job?.production_run_id"),
        )
        group_end = javascript.index("function jobBatchCounts(batch)", key_end)
        group_contract = javascript[key_end:group_end]
        self.assertIn("const key = jobBatchKey(job);", group_contract)
        self.assertIn("if (!groups.has(key))", group_contract)
        self.assertIn("const batch = groups.get(key);", group_contract)
        self.assertIn("batch.jobs.push(job);", group_contract)
        self.assertIn("data-toggle-job-batch", javascript)
        self.assertIn("data-open-job-batch-records", javascript)
        self.assertIn("batch.summary.overall_progress", javascript)
        self.assertIn("batch.summary.unfinished ?? batch.summary.active", javascript)
        self.assertIn("当前显示 ${batch.jobs.length}/${total} 个视频任务", javascript)
        self.assertIn("job.batch_total_count", javascript)
        self.assertIn("function hasPollableWork", javascript)
        self.assertIn("const activeVideoTotal = activeBatches.reduce", javascript)
        self.assertIn("counts.queued += Number(batch.summary.queued", javascript)
        self.assertIn("counts.active += Number(batch.summary.running", javascript)
        self.assertEqual(javascript.count("${retryAction}"), 1)
        self.assertIn("state.jobBatchDisclosure.set(disclosureKey, nextOpen)", javascript)
        self.assertIn("Number(batch.summary?.unfinished ?? batch.summary?.active ?? 0) > 0", javascript)
        self.assertIn('state.recordBatchFilter = String(batchRecords.dataset.openJobBatchRecords || "")', javascript)
        self.assertIn("当前批次 · ${String(current).slice(0, 10)}", javascript)
        self.assertIn('await loadProductionRecordGroups()', javascript)
        self.assertIn(".job-batch-head", css)
        self.assertIn(".job-batch-body[hidden]", css)
        self.assertIn(".job-batch-toggle:focus-visible", css)

    def test_batch_cards_open_the_most_specific_publish_directory(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        helper_start = javascript.index("function jobResolvedOutputFolder(job)")
        helper_end = javascript.index("function batchResolvedOutputFolder(jobs)", helper_start)
        helper = javascript[helper_start:helper_end]
        self.assertLess(
            helper.index("publish_batch_folder"),
            helper.index("output_file"),
        )
        self.assertLess(
            helper.index("output_file"),
            helper.index("output_folder"),
        )
        self.assertIn("outputFolder: batchResolvedOutputFolder(batch.jobs)", javascript)
        self.assertIn('class="job-batch-output" data-output-folder=', javascript)
        self.assertIn("打开本批文件夹", javascript)
        self.assertIn('class="record-batch-output" data-output-folder=', javascript)
        self.assertIn(".job-batch-actions", css)
        self.assertIn(".job-batch-output", css)
        self.assertIn(".record-batch-output", css)

    def test_production_preview_switches_between_live_style_scenes(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        for scene in ("intro", "subtitle", "outro"):
            self.assertIn(f'data-production-preview-scene="{scene}"', html)
            self.assertIn(f'[data-preview-scene="{scene}"]', css)
        self.assertIn('id="preview-outro-cover"', html)
        self.assertIn('id="preview-outro-caption"', html)
        self.assertIn('id="preview-outro"', html)
        self.assertIn('id="preview-outro" lang="en-US" hidden aria-hidden="true"', html)
        self.assertIn("function setProductionPreviewScene(scene)", javascript)
        self.assertIn("function productionPreviewSceneForControl(control)", javascript)
        self.assertIn("function applyProductionCardStyles(root, settings,", javascript)
        self.assertIn("function paintOutroCover(container, novel", javascript)
        for control_id in (
            "production-intro-card-enabled",
            "production-intro-card-duration",
            "production-intro-card-copy",
            "production-intro-card-preset",
            "production-code-card-preset",
            "production-subtitle-preset",
            "production-subtitle-animation",
            "production-outro-card-preset",
            "production-cover-animation",
        ):
            self.assertIn(control_id, javascript)
        for preset in (
            "editorial_white",
            "cover_story_dark",
            "cinematic_dark",
            "romance_soft",
            "minimal_clean",
            "brand_pill",
            "dark_glass",
            "light_chip",
            "outline_only",
            "clear_outline",
            "cinematic_shadow",
            "clean_minimal",
            "bold_drama",
            "reader_focus",
            "soft_box",
            "brand_focus",
        ):
            self.assertIn(f"preset-{preset}", css)
        self.assertIn('class="caption-word is-current"', html)
        self.assertIn('span.className = `caption-word', javascript)
        self.assertIn('preview.dataset.previewScene = next', javascript)
        self.assertIn('[data-intro-preset="cinematic_dark"]', css)
        self.assertIn('--subtitle-preview-color', javascript)
        self.assertIn('--subtitle-preview-background', javascript)
        self.assertIn('savedPreviewSeconds === 30 ? DEFAULT_PREVIEW_SECONDS', javascript)
        self.assertIn('assignValue("#production-subtitle-color"', javascript)
        self.assertIn('--subtitle-preview-outline-shadow', javascript)
        self.assertNotIn('subtitle.style.color = settings.subtitle?.text_color', javascript)
        self.assertNotIn(
            'subtitle.style.textShadow = `${-outlineWidth}px ${-outlineWidth}px 0 ${outline},',
            javascript,
        )
        self.assertIn('.preview-subtitle.preset-soft_box', css)
        self.assertIn('.preview-subtitle.preset-cinematic_shadow', css)
        self.assertIn('.preview-subtitle.animation-soft_pop', css)
        self.assertIn('@keyframes subtitle-soft-pop', css)
        self.assertIn('translateX(-50%) translateY(5px) scale(.97)', css)
        self.assertIn('.outro-cover-preview > img { display: block; object-fit: cover;', css)
        for animation in ("none", "fade", "gentle_push", "gentle_pull", "slow_pan", "soft_parallax"):
            self.assertIn(f'value="{animation}"', html + javascript)

    def test_production_workbench_preserves_the_complete_batch_recipe(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        for element_id in (
            "production-workbench-title",
            "production-workbench-content",
            "library-production-picker",
        ):
            self.assertIn(f'id="{element_id}"', html)
        for production_function in (
            "function renderProductionWorkbench()",
            "function selectProductionNovel(novelId)",
            "function syncProductionDraftFromControls",
            "function productionDraftPayload",
            "function updateProductionPreview()",
        ):
            self.assertIn(production_function, javascript)
        for section_title in ("选择本批内容", "配音与节奏", "字幕与画面", "素材与输出", "确认并开始"):
            self.assertIn(section_title, javascript)
        self.assertIn('class="workbench-section workbench-direct-section"', javascript)
        self.assertIn('data-queue-production-draft', javascript)
        self.assertNotIn("mainSampleMarkup(novel", javascript)
        self.assertIn(".production-workbench", css)
        self.assertIn(".workbench-section", css)

    def test_production_quick_navigation_and_preview_feedback_are_wired(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        for section in ("content", "voice", "visual", "output"):
            self.assertIn(f'data-production-jump="{section}"', html)
            self.assertIn(f'data-production-jump-status="{section}"', html)
        self.assertIn("function productionMissingDescriptors(novel, draft)", javascript)
        self.assertIn("function focusProductionSection(section, selector", javascript)
        self.assertIn("function updateProductionJumpbar", javascript)
        self.assertIn("data-production-missing=", javascript)
        self.assertIn("function openProductionPreview()", javascript)
        self.assertIn("function replayProductionPreview()", javascript)
        self.assertIn("data-replay-production-preview", html)
        self.assertIn('event.target.closest("[data-production-preview-scene]")', javascript)
        self.assertIn("setProductionPreviewScene(previewScene.dataset.productionPreviewScene)", javascript)
        self.assertIn("function productionPreviewSceneForControl(control)", javascript)
        self.assertIn("resetProductionStyleFromPreset(event.target)", javascript)
        self.assertIn('#production-intro-card-enabled, #production-cover-outro-enabled', javascript)
        self.assertIn('productionJump.dataset.productionMissing === "platform-binding"', javascript)
        self.assertIn('await openNovelDetail(state.productionNovel.id, productionJump)', javascript)
        self.assertIn(".production-section-jumpbar {", css)
        self.assertIn("position: sticky", css)
        self.assertIn('[data-view="queue"] .preview-dock { top: 72px; }', css)
        self.assertIn(".production-focus-pulse", css)

    def test_personal_recipe_visibility_and_delete_permissions_are_normalized(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        self.assertIn("const isOwnedByCurrentUser = (item)", javascript)
        self.assertIn('String(item?.owner_user_id || "") === currentUserId', javascript)
        self.assertIn("return isOwnedByCurrentUser(item) || (includeManaged && !employee)", javascript)
        self.assertIn("deletable: owned || (!employee && includeManaged) || Boolean(item.deletable)", javascript)
        self.assertIn('data-delete-production-preset', javascript)
        self.assertIn('selected?.owned_by_current_user ? "删除我的方案" : "管理员删除"', javascript)

    def test_admin_cleanup_entrances_call_real_delete_apis(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="delete-software-user"', html)
        self.assertIn('id="delete-platform"', html)
        self.assertIn('id="delete-publishing-account"', html)
        self.assertIn('function deleteSelectedPublishingAccount(button)', javascript)
        self.assertIn('checkedCall("delete_publishing_account", account.id)', javascript)
        self.assertIn('data-toggle-code=', javascript)
        self.assertIn('data-delete-code=', javascript)
        self.assertIn('checkedCall("delete_promo_code", promoCodeId)', javascript)
        self.assertIn('data-action="delete-novel"', javascript)
        self.assertIn('checkedCall("delete_novel", novel.id)', javascript)
        self.assertGreaterEqual((html + javascript).count("settings-admin-only"), 5)

    def test_production_workspace_has_batch_slate_and_create_task_tabs(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('class="production-local-tabs"', html)
        self.assertIn('role="tablist"', html)
        for tab in ("create", "tasks"):
            self.assertIn(f'data-production-local-tab="{tab}"', html)
            self.assertIn(f'data-production-local-panel="{tab}"', html)
        self.assertIn('aria-selected="true"', html)
        self.assertIn('aria-selected="false"', html)
        self.assertIn("function setProductionLocalTab", javascript)
        self.assertIn("state.productionLocalTab", javascript)
        self.assertIn(".production-local-tabs", css)
        self.assertIn(".production-local-panel[hidden]", css)

        self.assertIn('class="production-batch-slate"', javascript)
        for item in ("novel", "mode", "recipe", "missing"):
            self.assertIn(f'data-batch-slate="{item}"', javascript)
        self.assertIn("productionMissingItems(novel, draft)", javascript)
        self.assertIn(".production-batch-slate", css)

    def test_production_content_is_two_by_two_and_sections_keep_collapsible_summaries(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('class="workbench-field-grid workbench-content-grid"', javascript)
        content_grid_start = css.index(".workbench-content-grid")
        content_grid_end = css.index("}", content_grid_start)
        content_grid_rule = css[content_grid_start:content_grid_end]
        self.assertRegex(
            content_grid_rule,
            r"grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)",
        )
        self.assertIn(".workbench-content-grid > .field", css)
        self.assertRegex(
            css,
            r"(?s)\.workbench-content-grid\s*>\s*\.field\s*\{[^}]*align-content:\s*start;",
        )
        self.assertRegex(
            css,
            r"(?s)\.workbench-content-grid\s*>\s*\.field\s*>\s*select,[^{]*\{[^}]*height:\s*var\(--production-touch-target\);",
        )

        for section in ("content", "voice", "visual", "output"):
            self.assertIn(f'data-production-section="{section}"', javascript)
            self.assertIn(f'data-production-section-summary="{section}"', javascript)
        self.assertIn("data-toggle-production-section", javascript)
        self.assertIn("aria-expanded=", javascript)
        self.assertIn("aria-controls=", javascript)
        self.assertIn("function setProductionSectionExpanded", javascript)
        self.assertIn(".workbench-section-summary", css)
        self.assertIn(".workbench-section-body[hidden]", css)

    def test_production_visual_editor_has_picture_and_caption_semantic_panels(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('class="production-visual-semantics"', javascript)
        for panel, title_id in (
            ("picture", "production-picture-panel-title"),
            ("captions", "production-captions-panel-title"),
        ):
            self.assertIn(f'data-visual-panel="{panel}"', javascript)
            self.assertIn(f'aria-labelledby="{title_id}"', javascript)
            self.assertIn(f'id="{title_id}"', javascript)
        self.assertIn(".production-visual-semantics", css)
        self.assertIn(".production-semantic-panel", css)
        self.assertIn('class="workbench-advanced"', javascript)

    def test_production_has_sticky_action_dock_and_narrow_preview_drawer(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('class="production-action-dock"', javascript)
        self.assertIn('data-queue-production-draft', javascript)
        self.assertIn('role="status"', javascript)
        self.assertIn('aria-live="polite"', javascript)
        self.assertRegex(
            css,
            r"(?s)\.production-action-dock\s*\{[^}]*position:\s*sticky;"
            r"[^}]*bottom:\s*(?:0|var\([^)]+\));",
        )

        self.assertIn('id="production-preview-drawer"', html)
        self.assertIn('class="production-preview-drawer-trigger"', html)
        self.assertIn("data-open-production-preview-drawer", html)
        self.assertIn('aria-controls="production-preview-drawer"', html)
        self.assertIn("data-close-production-preview-drawer", html)
        self.assertIn("function setProductionPreviewDrawerOpen", javascript)
        self.assertIn(".production-preview-drawer-trigger", css)
        self.assertIn(".preview-dock.is-drawer-open", css)

    def test_production_workbench_uses_44px_targets_and_container_queries(self) -> None:
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("--production-touch-target: 44px", css)
        self.assertIn("min-height: var(--production-touch-target)", css)
        self.assertIn("container-type: inline-size", css)
        self.assertIn("container-name: production-workbench", css)
        self.assertIn("@container production-workbench", css)
        container_query_start = css.index("@container production-workbench")
        container_query_contract = css[container_query_start:]
        for selector in (
            ".workbench-content-grid",
            ".production-visual-semantics",
            ".production-preview-drawer-trigger",
        ):
            self.assertIn(selector, container_query_contract)
        self.assertIn(".workbench-section-toggle:focus-visible", css)
        self.assertIn(".production-preview-drawer-trigger:focus-visible", css)

    def test_production_uses_visual_library_picker_and_story_type_classification(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertNotIn('id="production-novel-select"', html)
        self.assertIn('id="library-production-picker"', html)
        self.assertIn("data-open-production-picker", html)
        self.assertIn("data-cancel-production-picker", html)
        self.assertIn("data-select-production-novel", javascript)
        for function_name in (
            "productionNovelPickerMarkup",
            "enterProductionNovelPicker",
            "leaveProductionNovelPicker",
            "storyMoodOptions",
            "storyClassificationMarkup",
            "reclassifyProductionNovel",
        ):
            self.assertIn(f"function {function_name}", javascript)
        self.assertIn('id="voice-candidate-mood"', javascript)
        self.assertIn('checkedCall("classify_novel", novel.id', javascript)
        self.assertIn("story_mood: draft.story_mood", javascript)
        self.assertIn("story_mood_source: draft.story_mood_source", javascript)
        for mood in ("suspense", "romance", "sad", "revenge"):
            self.assertIn(f"{mood}:", javascript)
        self.assertIn(".production-novel-launch", css)
        self.assertIn(".story-classification-proof", css)

    def test_minimum_window_breakpoint_and_keyboard_focus_are_defined(self) -> None:
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("@media (max-width: 1360px)", css)
        self.assertIn("@media (max-height: 780px) and (min-width: 981px)", css)
        self.assertIn(".choice-card input:focus-visible + span", css)
        self.assertIn(".button:disabled", css)

    def test_primary_navigation_exposes_production_library_records_and_settings(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        expected_views = {
            "queue": "制作台",
            "library": "资料库",
            "records": "生产记录",
            "settings": "设置",
        }
        for view, label in expected_views.items():
            self.assertIn(f'data-view-target="{view}"', html)
            self.assertIn(label, html)
        for view in ("styles", "providers", "hub", "accounts"):
            self.assertIn(f'data-open-view="{view}"', html)
            self.assertIn(f'data-view="{view}"', html)
        for view in ("library", "platforms", "publishing"):
            self.assertIn(f'data-resource-view="{view}"', html)
            self.assertIn(f'data-view="{view}"', html)
        self.assertIn('["platforms", "publishing"].includes(viewName)', javascript)

    def test_employee_settings_keep_personal_tools_and_hide_team_administration(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        # Settings is a real employee destination; it must not disappear with
        # the team-management controls nested below it.
        self.assertIn('data-view-target="settings"', html)
        self.assertIn('id="settings-nav-copy"', html)
        self.assertIn("个人方案 · 本机维护", javascript)
        self.assertNotIn(
            'body.web-runtime:not(.web-can-admin) [data-view-target="settings"]',
            css,
        )
        self.assertIn(
            'body.web-runtime:not(.web-can-admin) .settings-admin-only',
            css,
        )
        self.assertIn('class="settings-start settings-admin-only panel"', html)
        self.assertIn(
            'class="settings-entry settings-admin-only panel" data-open-view="hub"',
            html,
        )
        self.assertIn(
            'class="settings-entry settings-admin-only panel" data-open-view="accounts"',
            html,
        )
        self.assertIn('data-settings-entry="styles"', html)
        self.assertIn('data-settings-entry="local-maintenance"', html)
        self.assertIn("function renderSettingsAccess()", javascript)
        self.assertIn("本机维护与软件更新", javascript)
        self.assertIn("不会覆盖团队默认", javascript)

        # Employee style editing has its own local scope and never falls
        # through to the global save_settings call.
        self.assertIn('state.styleEditingScope = personal ? "personal" : "global"', javascript)
        self.assertIn('state.styleEditingScope === "personal"', javascript)
        self.assertIn('stableId: "style-personal-default"', javascript)
        self.assertIn("function customStyleStorageKey()", javascript)
        self.assertIn("personalVisualDefaults", javascript)
        self.assertIn('data-style-scope-step="personal"', html)
        personal_branch = javascript.index('if (state.styleEditingScope === "personal")')
        global_save = javascript.index('checkedCall("save_settings", stylePayload())', personal_branch)
        self.assertLess(personal_branch, global_save)

    def test_library_controls_import_sources_and_detail_drawer_are_present(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        for element_id in (
            "library-search",
            "library-platform-filter",
            "novel-list",
            "novel-detail-drawer",
            "novel-detail-content",
            "novel-import-dialog",
            "record-list",
            "record-failure-summary",
        ):
            self.assertIn(f'id="{element_id}"', html)
        for source in ("paste", "txt", "docx"):
            self.assertIn(f'data-import-source="{source}"', html)
        self.assertIn('data-library-layout="cards"', html)
        self.assertIn('data-library-layout="table"', html)
        self.assertIn('data-start-production-from-novel', html)
        self.assertIn("固定资料确认完成", html)
        self.assertIn("function renderNovelLibrary()", javascript)
        self.assertIn("function renderNovelDetail()", javascript)
        self.assertIn("function renderRecords()", javascript)

    def test_novel_languages_are_detected_filtered_and_safely_correctable(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="library-language-filter"', html)
        self.assertIn('data-language-filter=""', html)
        self.assertIn('<option value="">自动识别（推荐）</option>', html)
        self.assertIn('<option value="zh-Hans">简体中文</option>', html)
        self.assertIn('<option value="zh-Hant">繁体中文</option>', html)
        self.assertIn("自动识别主要语种", html)
        for function_name in (
            "novelLanguageInfo",
            "languageEditorCode",
            "renderLibraryLanguageFilter",
            "languageBadgeMarkup",
            "redetectNovelLanguage",
            "workbenchLanguageNoticeMarkup",
            "syncImportLanguageOptions",
        ):
            self.assertIn(f"function {function_name}", javascript)
        self.assertIn("novel?.language_detection", javascript)
        self.assertIn("state.libraryLanguageFilter", javascript)
        self.assertIn('payload.language = languageOverride', javascript)
        self.assertIn('redetect_language: true', javascript)
        self.assertIn('["it", "意大利语"]', javascript)
        self.assertIn('["hi", "印地语"]', javascript)
        self.assertIn('new Set(["en", "ja", "es", "fr", "hi", "it", "pt", "zh"])', javascript)
        self.assertIn('hi: "hi-IN"', javascript)
        self.assertIn('it: "it-IT"', javascript)
        self.assertIn('languageInfo.key === "en" ? "American female" : `${languageInfo.label}女声`', javascript)
        self.assertIn("已支持${escapeHtml(info.label)}免费本地配音", javascript)
        self.assertIn("先确认这部小说的语种", javascript)
        self.assertIn(".language-filter-options", css)
        self.assertIn(".language-badge.is-low-confidence", css)
        self.assertIn(".novel-language-editor", css)
        self.assertIn(".workbench-language-alert", css)
        for unsupported_label in ("俄语", "阿拉伯语", "泰语", "越南语"):
            self.assertNotIn(unsupported_label, html + javascript)

    def test_episode_duration_uses_stored_seconds_when_no_wpm_override_is_given(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        self.assertIn("if (units && requestedSpeed > 0)", javascript)
        self.assertIn("const storedDuration = Math.max(0, Number(episode?.duration_seconds)", javascript)
        self.assertNotIn("Math.max(1, Number(wpm) || 0)", javascript)

    def test_publishing_accounts_are_a_first_class_resource_library_view(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('data-view="publishing"', html)
        for element_id in (
            "new-publishing-account",
            "publishing-account-count",
            "publishing-platform-filter",
            "publishing-account-list",
            "publishing-editor-title",
            "publishing-account-form",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("发布账号库", html)
        self.assertIn("不登录 TikTok，也不自动发布", html)
        for function_name in (
            "renderPublishingAccountOptions",
            "resetPublishingAccountEditor",
            "editPublishingAccount",
            "renderPublishingAccounts",
        ):
            self.assertIn(f"function {function_name}", javascript)
        self.assertIn('checkedCall("save_publishing_account"', javascript)
        self.assertIn("payload.expected_version = Number(currentAccount.row_version)", javascript)
        self.assertIn(".publishing-account-list", css)
        self.assertIn(".publishing-account-item", css)
        self.assertIn(".publishing-empty", css)

    def test_library_bridge_contract_and_mock_mutations_cover_phase_one(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        methods = (
            "get_library_bootstrap",
            "choose_file",
            "import_novel_text",
            "import_novel_file",
            "get_novel",
            "save_novel",
            "save_novel_binding",
            "add_promo_code",
            "update_promo_code",
            "save_publishing_account",
            "save_production_draft",
            "generate_voice_candidates",
            "queue_production_draft",
            "approve_preview",
            "regenerate_preview",
            "retry_failed",
        )
        for method in methods:
            self.assertIn(f'"{method}"', javascript)
        self.assertIn("browserMockLibrary.novels.unshift", javascript)
        self.assertIn("binding.codes.length >= 5", javascript)
        self.assertIn("state.productionRecords.unshift", javascript)
        self.assertIn("当前桌面后端尚未提供", javascript)
        self.assertIn("function productionDraftPayload", javascript)
        self.assertIn(
            "target_video_count: audioOnly ? 1 : draft.target_video_count",
            javascript,
        )
        self.assertIn("production_settings: structuredClone(draft.production_settings", javascript)
        self.assertIn("voice: structuredClone(draft.voice", javascript)
        self.assertIn('libraryBackendError: ""', javascript)
        self.assertIn("data-retry-library", javascript)
        self.assertIn("小说库暂时无法读取", javascript)
        self.assertNotIn("当前桌面版缺少 get_library_bootstrap", javascript)

    def test_batch_video_total_direct_generation_and_asset_usage_are_wired(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="production-target-count"', javascript)
        self.assertIn("draft.target_video_count", javascript)
        self.assertIn("所选分集合并为一段完整正文", javascript)
        self.assertIn("同一内容生成多少个素材版本", javascript)
        self.assertNotIn("系统会均匀分配到已选分集", javascript)
        self.assertIn("const targetMinimum = 1;", javascript)
        self.assertIn("const jobs = Array.from({ length: targetCount }", javascript)
        self.assertIn("episode_ids: selected.map((episode) => episode.id)", javascript)
        self.assertIn(
            "variant_count: audioOnly ? 1 : Math.max(1, draft.target_video_count)", javascript
        )
        self.assertIn('join("\\n\\n")', javascript)
        self.assertNotIn("selected.flatMap", javascript)
        self.assertNotIn(
            "Math.ceil(draft.target_video_count / draft.episode_ids.length)",
            javascript,
        )
        self.assertIn('class="workbench-episode', javascript)
        self.assertIn('class="button button-primary production-direct-button"', javascript)
        self.assertIn("直接生成完整视频", javascript)
        self.assertIn("右侧即时预览确认后", javascript)
        self.assertNotIn("function mainSampleMarkup", javascript)
        self.assertNotIn('data-approve-preview=', javascript)
        self.assertNotIn('data-regenerate-preview=', javascript)
        self.assertNotIn("usage_count) >= 8", javascript)
        self.assertIn("暂无素材使用记录", javascript)
        self.assertIn('id="record-failed-count"', html)
        self.assertIn(".workbench-episode-grid", css)
        self.assertNotIn(".material-usage.is-high", css)
        self.assertNotIn(".record-material.is-high", css)
        self.assertIn("@media (max-width: 980px)", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)

    def test_real_preview_batch_voice_and_folder_recipe_contract_are_wired(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("voice_candidates) ? novel.voice_candidates.slice(0, 3)", javascript)
        self.assertIn("<audio controls preload=", javascript)
        self.assertIn('checkedCall("generate_voice_candidates", novel.id, mood)', javascript)
        self.assertIn('name="production-voice"', javascript)
        self.assertIn("draft.voice = { provider: candidate.provider", javascript)
        self.assertIn('"worker_runtime_snapshot"', javascript)
        self.assertIn('"worker_self_check"', javascript)
        self.assertIn('"get_queue_connection"', javascript)
        self.assertIn("正在自动重连", javascript)
        self.assertIn("LOCAL_WORKER_PROTOCOL_VERSION", javascript)
        self.assertIn("LOCAL_WORKER_PROTOCOL_VERSION = 3", javascript)
        self.assertIn("LOCAL_WORKER_MIN_COMPATIBLE_PROTOCOL_VERSION = 3", javascript)
        self.assertIn("function localWorkerCompatibility", javascript)
        self.assertIn("connection.data.runtime", javascript)
        self.assertIn("function localTtsRuntimeSnapshot", javascript)
        self.assertIn("effectiveTtsProviders().tts_provider", javascript)
        self.assertIn("ttsProviderLabel(candidate.provider)", javascript)
        self.assertIn("已使用 ${ttsProviderLabel(actualProvider)}", javascript)
        self.assertIn("试听后选择一个；本批所有分集保持同一声音。", javascript)
        for key in ("video_folder", "music_folder", "output_folder"):
            self.assertIn(f"{key}: {{", javascript)
            self.assertIn(f"{key}: draft.{key}", javascript)
        self.assertIn('data-draft-path="${key}"', javascript)
        self.assertIn('checkedCall("queue_production_draft", queuePayload)', javascript)
        self.assertIn("preview_required: false", javascript)
        self.assertNotIn('checkedCall("approve_preview"', javascript)
        self.assertNotIn('checkedCall("regenerate_preview"', javascript)
        self.assertIn("waiting_preview", javascript)
        self.assertIn("awaiting_approval", javascript)
        self.assertIn("interrupted", javascript)
        self.assertIn(".production-direct-launch", css)
        self.assertIn(".production-direct-button", css)
        self.assertIn(".production-voice-option", css)

    def test_queued_batch_detaches_editor_for_continuous_batch_creation(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("function freshProductionDraftForNextBatch", javascript)
        self.assertIn("function beginNextProductionBatch", javascript)
        fresh_start = javascript.index("function freshProductionDraftForNextBatch")
        fresh_end = javascript.index("function beginNextProductionBatch", fresh_start)
        fresh_contract = javascript[fresh_start:fresh_end]
        self.assertIn("...previous", fresh_contract)
        for cleared_field, empty_value in (
            ("id", '""'),
            ("promo_code_id", '""'),
            ("publishing_account_id", '""'),
            ("episode_ids", "[]"),
            ("intro_card_text", '""'),
            ("intro_card_source", '""'),
            ("intro_card_copies", "{}"),
        ):
            self.assertIn(f"{cleared_field}: {empty_value}", fresh_contract)
        for preserved_field in (
            "voice",
            "production_settings",
            "video_folder",
            "music_folder",
            "output_folder",
        ):
            self.assertNotIn(f"{preserved_field}:", fresh_contract)
        begin_end = javascript.index("function productionPreviewSeconds", fresh_end)
        begin_contract = javascript[fresh_end:begin_end]
        self.assertIn("const editableNovel = structuredClone(latest);", begin_contract)
        self.assertIn("upsertNovel(editableNovel);", begin_contract)
        payload_start = javascript.index("function productionDraftPayload")
        payload_end = javascript.index("function applySavedDraftResult", payload_start)
        self.assertIn("novel_id: novel.id", javascript[payload_start:payload_end])
        self.assertIn("const editableNovel = structuredClone(latest);", javascript)
        self.assertIn("upsertNovel(editableNovel);", javascript)
        self.assertIn(
            "beginNextProductionBatch(state.productionNovel, queuedDraft, { render: false, focus: false });",
            javascript,
        )
        self.assertNotIn(
            'draft.promo_code_id = activeCodes[0]?.id || "";', javascript
        )
        self.assertIn(
            "if (draft.promo_code_id && !activeCodes.some", javascript
        )
        self.assertIn("if (!draft?.id) return [];", javascript)
        self.assertIn("现在可以继续建立下一批", javascript)
        self.assertIn("data-start-next-production-batch", javascript)
        self.assertIn("上一批已加入队列，当前已是全新的制作批次", javascript)
        self.assertIn(".workbench-next-batch-note", css)

    def test_fully_cancelled_batch_never_uses_completed_copy(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        state_start = javascript.index("const batchState = allTerminal")
        state_end = javascript.index("const shortBatchId", state_start)
        state_contract = javascript[state_start:state_end]
        cancelled_guard = "counts.cancelled >= total && counts.completed === 0"
        self.assertIn(cancelled_guard, state_contract)
        self.assertIn('? "已全部取消"', state_contract)
        self.assertIn(': "已全部完成"', state_contract)
        self.assertLess(
            state_contract.index(cancelled_guard),
            state_contract.index(': "已全部完成"'),
        )

    def test_queued_batch_is_labelled_waiting_instead_of_running(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        state_start = javascript.index("const needsApproval = batch.summary")
        state_end = javascript.index("const shortBatchId", state_start)
        state_contract = javascript[state_start:state_end]
        self.assertIn("Number(batch.summary.running || 0) > 0", state_contract)
        self.assertNotIn("const isLive = counts.active > 0", state_contract)
        self.assertIn('? "等待确认"', state_contract)
        self.assertIn('? "正在制作"', state_contract)
        self.assertIn(': "等待制作"', state_contract)

    def test_cover_summary_revision_and_hub_settings_are_exposed(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        self.assertIn('data-choose-novel-cover', javascript)
        self.assertIn('checkedCall("choose_file", "cover")', javascript)
        self.assertIn("cover_path: coverPath", javascript)
        self.assertIn("novel.cover_uri", javascript)
        self.assertIn("function coverImageMarkup", javascript)
        self.assertIn('class="cover-image-backdrop"', javascript)
        self.assertIn('class="cover-image-main"', javascript)
        self.assertIn('novel.cover_uri ? coverImageMarkup(novel)', javascript)
        self.assertIn(".novel-cover-art.has-cover-image", (ROOT / "ui" / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("object-fit: contain", (ROOT / "ui" / "styles.css").read_text(encoding="utf-8"))
        self.assertIn('data-import-synopsis', javascript)
        self.assertIn('checkedCall("choose_file", "summary")', javascript)
        self.assertIn('checkedCall("read_text_document", filePath)', javascript)
        self.assertIn('data-update-manuscript', javascript)
        self.assertIn("payload.novel_id = state.importTargetNovelId", javascript)

        for element_id in (
            "hub-mode",
            "hub-device-name",
            "hub-endpoint",
            "hub-listen-port",
            "hub-host-connection",
            "hub-host-endpoint",
            "copy-hub-endpoint",
            "hub-client-web-connection",
            "hub-client-web-endpoint",
            "copy-hub-client-web-endpoint",
            "check-hub-status",
            "save-hub-settings",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertNotIn('id="hub-share-previews"', html)
        self.assertNotIn('id="hub-share-narration"', html)
        self.assertIn("成片与旁白只保存在制作电脑", html)
        self.assertIn("share_previews: false", javascript)
        self.assertIn("share_narration: false", javascript)
        self.assertIn('checkedCall("get_hub_status")', javascript)
        self.assertIn('checkedCall("save_settings", hubSettingsPayload())', javascript)
        self.assertIn("保存后重启软件生效", html)
        self.assertIn("团队访问地址", html)
        self.assertIn("浏览器也可直接打开登录", html)
        self.assertIn("主电脑必须保持 StoryForge 运行", html)
        self.assertIn("浏览器可直接打开并登录", javascript)
        self.assertNotIn("网页可用素材根目录", html)
        self.assertNotIn('id="hub-web-allowed-roots"', html)
        self.assertIn("主电脑只记录小说、批次、进度、结果和本机文件引用", html)
        self.assertIn('runtimeMode === "host"', javascript)
        self.assertIn("Boolean(data?.running)", javascript)
        self.assertIn("copyEndpoint.dataset.endpoint", javascript)
        self.assertIn("data?.client_web_url", javascript)
        self.assertIn("data?.client_web_running", javascript)
        self.assertIn("本机制作网址已复制", javascript)
        self.assertIn("navigator.clipboard.writeText(endpoint)", javascript)

    def test_hub_device_management_requires_a_running_host(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        # Saving a configured mode is not enough: authorization stays locked until
        # the running Hub is actually a healthy host and no restart is pending.
        self.assertIn("status.runtime_mode || status.mode", javascript)
        self.assertIn('restartRequired ? "等待重启生效"', javascript)
        self.assertIn('runtimeMode === "host"', javascript)
        self.assertIn("Boolean(status.running)", javascript)
        self.assertIn("status.restart_required", javascript)
        self.assertIn("state.hubRuntimeStatus = data.hub_status || null", javascript)
        self.assertIn('state.hubRuntimeStatus = await checkedCall("get_hub_status")', javascript)

    def test_record_artifact_preview_uses_catalog_uris_and_lists_sidecars(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        for element_id in (
            "record-artifact-dialog",
            "record-artifact-title",
            "record-artifact-video",
            "record-artifact-video-empty",
            "record-artifact-source",
            "record-artifact-list",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('data-duration="15"', html)
        self.assertIn("Number(record.artifact_count || 0) > 0", javascript)
        self.assertIn("data-view-record-artifacts", javascript)
        self.assertIn('checkedCall("get_record_artifacts", recordId)', javascript)
        self.assertIn('method === "get_record_artifacts"', javascript)
        self.assertIn('artifact.kind === "sample" && artifact._media_uri', javascript)
        self.assertIn("preview_narration", javascript)
        self.assertIn("preview_alignment", javascript)
        self.assertIn("<audio controls preload=", javascript)
        self.assertIn(".artifact-dialog-body", css)
        self.assertIn(".artifact-video-frame video", css)

    def test_global_caption_render_and_cover_policies_load_preview_and_save(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        control_ids = (
            "caption-mode",
            "subtitle-preset",
            "subtitle-animation",
            "render-mode",
            "cover-animation",
        )
        for element_id in control_ids:
            self.assertIn(f'id="{element_id}"', html)
            self.assertIn(f'assignValue("#{element_id}"', javascript)
        self.assertIn('id="cover-outro-enabled"', html)
        self.assertIn('assignChecked("#cover-outro-enabled"', javascript)

        for option_value in (
            "sentence",
            "semantic",
            "clear_outline",
            "cinematic_shadow",
            "clean_minimal",
            "bold_drama",
            "soft_pop",
            "speed",
            "quality",
            "compatibility",
            "gentle_push",
            "gentle_pull",
            "slow_pan",
            "soft_parallax",
            "fade",
            "none",
        ):
            self.assertIn(f'value="{option_value}"', html)
        for property_name in (
            "caption_mode",
            "subtitle_preset",
            "subtitle_animation",
            "render_mode",
            "cover_outro_enabled",
        ):
            self.assertIn(f'{property_name}: $("#', javascript)
        self.assertIn('cover_animation: normalizedCoverAnimation($("#cover-animation").value)', javascript)
        self.assertNotIn('value="soft_shadow"', html)
        self.assertNotIn('value="editorial_box"', html)
        self.assertNotIn('value="high_contrast"', html)
        self.assertIn("root.dataset.subtitlePreset", javascript)
        self.assertIn("root.dataset.coverAnimation = coverAnimation", javascript)
        self.assertIn('paintOutroCover($("#preview-outro-cover"), novel, coverAnimation)', javascript)
        self.assertIn('paintOutroCover($("#style-outro-cover")', javascript)
        self.assertIn(".preview-subtitle.preset-cinematic_shadow", css)
        self.assertIn('.outro-cover-preview[data-cover-animation="gentle_push"]', css)
        self.assertIn("object-fit: cover", css)
        self.assertIn('name="production-output-mode"', javascript)
        self.assertIn('"video_and_mp3"', javascript)
        self.assertIn('"audio_only"', javascript)
        self.assertIn('"reuse_audio"', javascript)
        self.assertIn('productionSettings.output_mode =', javascript)
        self.assertIn('常规视频生成', javascript)
        self.assertIn('仅生成配音', javascript)
        self.assertIn('已有配音更换素材', javascript)
        self.assertIn('生成完整视频', javascript)
        self.assertNotIn('生成完整视频 + 配音', javascript)
        self.assertIn('生成纯旁白配音', javascript)
        self.assertNotIn('生成完整视频 + MP3', javascript)
        self.assertIn('"只生成纯旁白 MP3，不读取视频素材和背景音乐。"', javascript)
        self.assertNotIn('value="video_only"', javascript)
        self.assertIn('!audioOnly && !draft.video_folder', javascript)
        self.assertIn('bgmMode === "auto" && !draft.music_folder', javascript)

    def test_production_modes_speed_audio_and_employee_memory_contract(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

        self.assertIn(
            'const PRODUCTION_OUTPUT_MODES = new Set(["video_and_mp3", "audio_only", "reuse_audio"])',
            javascript,
        )
        for label in ("常规视频生成", "仅生成配音", "已有配音更换素材"):
            self.assertIn(label, javascript)
        self.assertIn('id="production-source-narration-audio"', javascript)
        self.assertIn('source_narration_audio: String(draft.source_narration_audio || "")', javascript)
        self.assertIn('source_narration_audio: payload.source_narration_audio', javascript)
        self.assertIn('field === "source_narration_audio" ? "narration_source" : "audio"', javascript)
        self.assertIn("StoryForge 输出的 MP3 配音或成品视频", javascript)

        self.assertIn("const PRODUCTION_WPM_PRESETS = [220, 240, 260, 280]", javascript)
        self.assertIn('id="production-wpm-custom" type="number" min="200" max="280"', javascript)
        self.assertIn('checkedCall("generate_voice_candidates", novel.id, mood, wpm)', javascript)
        self.assertIn("function stopProductionWpmPreview", javascript)
        self.assertIn("不会播放模拟声音", javascript)

        self.assertIn("const PRODUCTION_VIDEO_SPEED_PRESETS = [1.0, 1.1, 1.25, 1.4, 1.5]", javascript)
        self.assertIn('id="production-video-speed-custom" type="number" min="0.8" max="3.0"', javascript)
        self.assertIn("productionSettings.video_playback_speed = selectedProductionVideoSpeed()", javascript)
        self.assertIn('id="production-video-transition"', javascript)
        self.assertIn('value="fade">淡化衔接（固定 0.2 秒）', javascript)
        self.assertIn('productionSettings.video_transition =', javascript)

        self.assertIn('id="production-subtitle-word-mode"', javascript)
        self.assertIn('value="cumulative">逐词累积高亮', javascript)
        self.assertIn('value="single">单词逐个出现', javascript)
        self.assertIn("productionSettings.subtitle_word_mode =", javascript)
        self.assertIn('wordMode === "single"', javascript)
        self.assertIn("productionSettings.export_narration_audio = false", javascript)

        for bgm_mode in ("auto", "manual", "none"):
            self.assertIn(f'name="production-bgm-mode" value="{bgm_mode}"', javascript)
        self.assertIn('id="production-bgm-file"', javascript)
        self.assertIn("productionSettings.bgm_mode =", javascript)
        self.assertIn("productionSettings.bgm_file", javascript)

        self.assertIn('const PRODUCTION_PREFERENCE_STORAGE_KEY = "storyforge.production-preferences.v1"', javascript)
        self.assertIn("function productionPreferenceStorageKey", javascript)
        self.assertIn("preferences.last_settings", javascript)
        self.assertIn("novelPreferences.promo_codes", javascript)
        self.assertIn("preferences.publishing_accounts", javascript)
        self.assertIn("window.localStorage.setItem(productionPreferenceStorageKey()", javascript)

        self.assertIn(".production-mode-router", css)
        self.assertIn('.studio-grid[data-production-mode="audio_only"] > .preview-dock', css)
        self.assertIn(".production-audio-proof", css)
        self.assertIn(".production-file-picker", css)
        self.assertIn("@media (max-width: 680px)", css)
        self.assertNotIn("解压视频素材", html + javascript + css)
        self.assertNotIn("智能速度", html + javascript + css)

    def test_video_template_default_and_per_batch_controls_are_wired(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="video-template"', html)
        self.assertIn('<option value="classic">经典模板</option>', html)
        self.assertIn(
            '<option value="platform_story_card">平台简介卡（推荐）</option>',
            html,
        )
        self.assertIn('id="default-template-proof"', html)
        self.assertIn('assignValue("#video-template", settings.video_template', javascript)
        self.assertIn('video_template: $("#video-template").value', javascript)
        self.assertIn('id="production-intro-card-enabled"', javascript)
        self.assertIn('id="production-intro-card-duration"', javascript)
        self.assertIn('id="production-intro-card-copy"', javascript)
        self.assertIn("productionSettings.intro_card_duration_seconds = Math.max", javascript)
        self.assertIn("production_settings: structuredClone(draft.production_settings || {})", javascript)
        self.assertIn(
            'productionSettings.video_template = productionSettings.intro_card_enabled ? "platform_story_card" : "classic"',
            javascript,
        )
        self.assertIn("video_template: defaults.video_template", javascript)
        self.assertIn("root.dataset.videoTemplate = videoTemplate", javascript)
        self.assertIn('event.target.matches(\'input[name="production-voice"]\')', javascript)
        self.assertIn(
            '.video-preview[data-video-template="platform_story_card"]',
            css,
        )
        self.assertIn(
            '.production-template-row[data-template="platform_story_card"]',
            css,
        )
        self.assertIn('id="preview-story-search"', html)
        self.assertNotIn('class="story-ticket-kicker">SEARCH</span>', html)
        self.assertIn('id="preview-story-platform-name">Platform', html)
        self.assertIn('id="preview-story-search">Search “123456”', html)
        self.assertIn('.video-preview.production-preview > .preview-subtitle', css)
        self.assertIn('.video-preview.style-preview > .preview-subtitle', css)
        self.assertIn("function normalizedSubtitlePreviewLayout", javascript)
        self.assertIn("function estimateSubtitleBlockWidth", javascript)
        self.assertIn("function applySubtitlePreviewStyles", javascript)
        self.assertGreaterEqual(javascript.count("applySubtitlePreviewStyles("), 5)
        self.assertNotIn('STORY BRIEF', html)
        self.assertNotIn('id="preview-story-title"', html)
        self.assertNotIn("STORY PREVIEW", html + javascript + css)
        self.assertNotIn("story-intro-window", html + css)
        self.assertNotIn("story-card-meta", html + css)
        self.assertIn(
            "inset: 7.8125% 17.4074% 18.75%",
            css,
        )
        self.assertNotIn(
            '.video-preview[data-video-template="platform_story_card"] .preview-subtitle { left:',
            css,
        )
        self.assertIn("width: 65.1852%; max-width: 65.1852%", css)
        self.assertIn(
            ".story-summary-card { position: absolute; top: 29%; left: 50%; box-sizing: border-box",
            css,
        )
        self.assertIn("width: 65.1852%; max-width: 65.1852%", css)
        self.assertIn("function normalizedIntroPreviewGeometry", javascript)
        self.assertIn("function applyIntroPreviewGeometry", javascript)
        self.assertGreaterEqual(javascript.count("applyIntroPreviewGeometry("), 3)
        self.assertIn("finitePreviewNumber(intro.position_x_percent, 50)", javascript)
        self.assertIn("introPreviewSafeArea.widthPercent", javascript)
        self.assertIn("transform: translateX(-50%) translateY(18px)", css)
        self.assertIn("-webkit-line-clamp: 2", css)
        self.assertIn("-webkit-line-clamp: 5", css)
        self.assertIn(
            '`${platform?.name || "Platform"} · Search “${code}”`',
            javascript,
        )
        self.assertIn("characters.slice(0, 48)", javascript)
        self.assertIn("words.slice(0, 20)", javascript)
        self.assertIn(r"\u2e80-\u9fff", javascript)

    def test_platform_logo_branding_is_configurable_and_shared_by_previews(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        for element_id in (
            "platform-logo-preview",
            "platform-logo-path",
            "choose-platform-logo",
            "clear-platform-logo",
            "preview-story-platform-mark",
            "style-story-platform-mark",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('name="logo_path"', html)
        self.assertIn('name="brand_color"', html)
        self.assertIn("不会使用小说封面代替", html)
        self.assertIn("function platformLogoSource(platform)", javascript)
        self.assertIn(
            'platform?.logo_uri || platform?.logo_path || ""', javascript
        )
        self.assertIn("function platformLogoMarkup(platform", javascript)
        self.assertIn("function choosePlatformLogo(button)", javascript)
        self.assertIn('checkedCall("choose_file", "cover")', javascript)
        self.assertIn('form.elements.logo_path.value = String(logoPath)', javascript)
        self.assertIn("function clearPlatformLogo()", javascript)
        self.assertIn("platformLogoMarkup(platform)", javascript)
        self.assertIn('$("#preview-story-platform-mark")', javascript)
        self.assertIn('paintPlatformLogo($("#style-story-platform-mark")', javascript)
        choose_body = javascript[
            javascript.index("async function choosePlatformLogo"):
            javascript.index("function clearPlatformLogo")
        ]
        self.assertNotIn("state.selectedNovel", choose_body)
        self.assertNotIn('checkedCall("save_novel"', choose_body)
        for selector in (
            ".platform-brand-editor",
            ".platform-logo-preview",
            ".story-card-platform-mark",
        ):
            self.assertIn(selector, css)

    def test_software_accounts_permissions_are_operable_without_token_ui(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('data-open-view="accounts"', html)
        self.assertIn('data-view="accounts"', html)
        for element_id in (
            "software-user-list",
            "software-user-form",
            "software-user-permissions",
        ):
            self.assertIn(f'id="{element_id}"', html)
        for removed_id in (
            "issue-user-token",
            "account-token-list",
            "hub-token-dialog",
            "issued-hub-token",
            "hub-access-token",
            "connect-hub-token",
        ):
            self.assertNotIn(f'id="{removed_id}"', html)
        for permission in (
            "library.edit",
            "promo_codes.manage",
            "records.view_all",
            "users.manage",
            "permissions.manage",
            "hub.manage",
        ):
            self.assertIn(permission, javascript)
        permission_catalog = javascript[
            javascript.index("const softwarePermissionCatalog") :
            javascript.index("const storyMoodCatalog")
        ]
        self.assertNotIn("samples.approve_all", permission_catalog)
        self.assertNotIn("样片", permission_catalog)
        for method in (
            "list_software_users",
            "save_software_user",
            "set_user_permission",
        ):
            self.assertIn(f'checkedCall("{method}"', javascript)
            self.assertIn(f'method === "{method}"', javascript)
        self.assertIn('checkedCall("delete_software_user"', javascript)
        self.assertNotIn("令牌", html)
        self.assertNotIn("生成授权码", html)
        for obsolete_code in (
            "list_hub_user_tokens",
            "issue_hub_user_token",
            "revoke_hub_user_token",
            "softwareUserTokens",
            "hubTokenUserId",
            "renderHubTokenManager",
            "connectHubWithLegacyToken",
        ):
            self.assertNotIn(obsolete_code, javascript)
        for obsolete_selector in (
            "hub-token",
            "account-token",
            "token-dialog",
            "issued-hub-token",
        ):
            self.assertNotIn(obsolete_selector, html)
            self.assertNotIn(obsolete_selector, css)
        self.assertIn("关闭后，该成员会立即无法登录", html)
        self.assertIn(".account-layout", css)
        self.assertIn(".permission-grid", css)

    def test_dynamic_device_fleet_is_lifecycle_only_and_presets_are_the_only_defaults(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        for element_id in (
            "managed-device-fleet",
            "managed-device-list",
            "managed-device-total",
            "managed-device-online",
            "managed-device-disabled",
            "refresh-managed-devices",
            "managed-device-dialog",
            "managed-device-form",
        ):
            self.assertIn(f'id="{element_id}"', html)
        for method in (
            "list_managed_devices",
            "get_managed_device",
            "rename_managed_device",
            "set_managed_device_active",
            "acknowledge_managed_device",
            "delete_managed_device",
        ):
            self.assertIn(f'checkedCall("{method}"', javascript)
            self.assertIn(f'method === "{method}"', javascript)

        combined_ui = html + javascript
        for obsolete_copy in ("下发制作默认值", "选择接收设置", "全选设备"):
            self.assertNotIn(obsolete_copy, combined_ui)
        for obsolete_selector in (
            "select-all-managed-devices",
            "push-managed-device-config",
            "managed-config-history",
            "device-sync-card",
            "device-sync-revision",
            "sync-device-config-now",
        ):
            self.assertNotIn(obsolete_selector, combined_ui)
        for obsolete_code in (
            "portableManagedDeviceConfigPayload",
            "renderManagedConfigHistory",
            "renderDeviceSyncStatus",
            "loadDeviceSyncStatus",
            "create_managed_device_config",
            "list_managed_device_configs",
            "get_managed_device_config",
            "get_device_sync_status",
            "sync_device_config_now",
        ):
            self.assertNotIn(obsolete_code, javascript)

        self.assertIn("function productionPresetItems", javascript)
        self.assertIn('id: "team-default"', javascript)
        self.assertIn('name: "团队默认"', javascript)
        self.assertIn('checkedCall("save_production_preset"', javascript)
        self.assertIn("20_000", javascript)
        self.assertIn("制作电脑", html)
        self.assertIn("团队制作方案在“制作台方案”中统一管理", html)
        self.assertIn(".managed-device-stat-rail", css)
        self.assertIn(".managed-device-workspace { display: block", css)
        self.assertIn(".managed-device-status", css)
        self.assertIn(".managed-device-new-login", css)
        for obsolete_style in (
            ".managed-config-",
            ".device-sync-",
            ".managed-device-config-state",
            ".managed-device-check",
        ):
            self.assertNotIn(obsolete_style, css)
        self.assertIn('id="delete-software-user"', html)

    def test_current_ui_hides_legacy_preview_approval_flow(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        self.assertNotIn("样片", html)
        self.assertNotIn("样片", javascript)
        self.assertNotIn('data-approve-preview=', javascript)
        self.assertNotIn('data-regenerate-preview=', javascript)
        self.assertNotIn("function mainSampleMarkup", javascript)
        self.assertIn("即时预览", html)
        self.assertIn("直接生成完整视频", javascript)
        self.assertIn("确认并开始", javascript)
        self.assertIn("preview_required: false", javascript)
        self.assertNotIn("兼容历史预览文件", html)
        self.assertIn("不上传视频、配音、预览或字幕对齐文件", html)
        self.assertIn('job.job_kind !== "preview"', javascript)
        self.assertIn("历史预览任务不再重试", javascript)

    def test_two_plain_language_roles_and_multi_computer_guide_are_exposed(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        for role, label in (
            ("admin", "管理员"),
            ("producer", "员工"),
        ):
            self.assertIn(f'value="{role}"', html)
            self.assertIn(label, html)
            self.assertIn(f'{role}: {{', javascript)
        self.assertNotIn('value="supervisor"', html)
        self.assertNotIn("supervisor: {", javascript)
        self.assertNotIn(".role-card-supervisor", css)
        self.assertIn("成员只分管理员和员工", html)
        self.assertIn("role-picker-two", html)
        self.assertIn('id="advanced-permissions"', html)
        self.assertIn('id="software-user-upgrade-notice"', html)
        self.assertIn('selectedSoftwareUser()?.role === "supervisor"', javascript)
        self.assertIn("按账号类型默认", javascript)
        self.assertIn("额外开放", javascript)
        self.assertIn("禁止使用", javascript)
        self.assertIn("多电脑协同", html)
        self.assertNotIn("三台电脑协同", html)
        self.assertIn('id="hub-port-field"', html)
        self.assertNotIn('id="hub-token-field"', html)
        self.assertIn(".role-picker", css)
        self.assertIn(".setup-steps", css)

    def test_visual_clarity_system_keeps_windows_text_readable(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI"', css)
        self.assertIn('"Segoe UI Variable Display", "Segoe UI", "Microsoft YaHei UI"', css)
        self.assertNotIn("-webkit-font-smoothing", css)
        self.assertIn("--radius-md: 11px", css)
        self.assertIn("Visual clarity pass", css)
        self.assertIn(".permission-row small { margin-top: 3px; font-size: 11px", css)
        self.assertIn(".sample-stage small { font-size: 10px", css)
        self.assertNotIn(".variant-swatch", css)
        self.assertIn(".software-user-item.is-disabled, .promo-code-ticket.is-inactive", css)
        self.assertIn('id="preview-code" lang="en-US"', html)
        self.assertIn('id="preview-subtitle" lang="en-US"', html)

    def test_novel_drawer_edits_fixed_data_and_launches_the_production_workbench(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        drawer_body = html.index('id="novel-detail-content"')
        drawer_footer = html.index('class="drawer-save-bar"')
        drawer_close = html.index("</aside>", drawer_body)
        self.assertLess(drawer_body, drawer_footer)
        self.assertLess(drawer_footer, drawer_close)
        self.assertNotIn('<footer class="drawer-save-bar"', javascript)
        self.assertIn('data-start-production-from-novel', html)
        self.assertIn("本批配音、字幕和素材在制作台设置", html)
        self.assertIn('class="detail-hero library-profile-hero"', javascript)
        self.assertIn("小说固定资料", javascript)
        self.assertIn("正文分集", javascript)
        self.assertIn("所选分集按正文顺序合成一个连续视频", javascript)
        self.assertIn("所选分集之间不重复回顾", javascript)
        self.assertIn("若从后续集开始，仅在整组开头回顾一次", javascript)
        self.assertIn("production-episode-selection-proof", javascript)
        self.assertNotIn("novel.episodes?.[0] ? [novel.episodes[0].id]", javascript)
        self.assertIn("平台绑定与历史口令", javascript)
        self.assertIn("制作时可自由多选并合成一条连续正文", javascript)
        self.assertIn('[data-start-production-from-novel], [data-produce-novel]', javascript)
        self.assertIn("grid-template-rows: 72px minmax(0,1fr) auto", css)
        self.assertIn(".drawer-save-bar { position: relative", css)
        self.assertIn(".library-chapter-list", css)
        self.assertIn(".library-binding-workbench", css)

    def test_merged_episode_duration_is_live_and_warns_without_blocking(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("const MERGED_DURATION_WARNING_SECONDS = 10 * 60", javascript)
        self.assertIn("function selectedEpisodeDurationSeconds", javascript)
        self.assertIn('id="production-episode-duration-warning"', javascript)
        self.assertIn("预计总时长", javascript)
        self.assertIn("超过 10 分钟", javascript)
        self.assertIn("只提示，不自动拆分，也不影响提交", javascript)
        self.assertIn(
            "durationWarning.hidden = selectedDurationSeconds <= MERGED_DURATION_WARNING_SECONDS",
            javascript,
        )
        self.assertIn(".workbench-duration-warning[hidden] { display: none; }", css)
        self.assertNotIn("selectedDurationSeconds > MERGED_DURATION_WARNING_SECONDS) missing.push", javascript)

    def test_style_studio_exposes_four_editable_visual_systems_and_live_preview(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("STYLE STUDIO / 样式工作室", html)
        for category in ("intro", "subtitle", "code", "outro"):
            self.assertIn(f'data-style-panel="{category}"', html)
            self.assertIn(f'data-style-editor-panel="{category}"', html)
            self.assertIn(f'data-style-preview-scene="{category}"', html)
        for control_id in (
            "intro-card-preset",
            "subtitle-preset",
            "code-card-preset",
            "outro-card-preset",
            "intro-font",
            "intro-opacity",
            "intro-width",
            "intro-x",
            "intro-y",
            "subtitle-font",
            "subtitle-outline-width",
            "subtitle-background-opacity",
            "subtitle-position-x",
            "code-font",
            "code-outline-width",
            "code-width",
            "code-x",
            "code-y",
            "outro-font",
            "outro-opacity",
            "outro-width",
            "outro-height",
            "outro-x",
            "outro-y",
            "custom-style-name",
            "save-custom-style-preset",
            "custom-style-presets",
        ):
            self.assertIn(f'id="{control_id}"', html)
        for preset in (
            "editorial_white",
            "cover_story_dark",
            "cinematic_dark",
            "romance_soft",
            "minimal_clean",
            "reader_focus",
            "soft_box",
            "brand_pill",
            "dark_glass",
            "light_chip",
            "outline_only",
            "brand_focus",
        ):
            self.assertIn(f'value="{preset}"', html)
        for function_name in (
            "applyStylePreset",
            "setStyleEditorPanel",
            "setStylePreviewScene",
            "captureVisualStyleSnapshot",
            "loadCustomStylePresets",
        ):
            self.assertIn(f"function {function_name}", javascript)
        self.assertIn("intro_card_preset:", javascript)
        self.assertIn("code_card_preset:", javascript)
        self.assertIn("outro_card_preset:", javascript)
        self.assertIn(".style-editor-tabs", css)
        self.assertIn(".style-preset-gallery", css)
        self.assertIn(".style-control-grid", css)

    def test_personal_production_presets_are_owned_compact_and_visible_in_workbench(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("自由配置（不套用方案）", javascript)
        self.assertIn("保存为我的方案", javascript)
        self.assertIn("function productionPresetManagementItems()", javascript)
        self.assertIn('item.scope === "curated" || item.scope === "team"', javascript)
        self.assertIn("const isOwnedByCurrentUser = (item) => Boolean(item?.owned_by_current_user)", javascript)
        self.assertIn("String(item?.owner_user_id || \"\") === currentUserId", javascript)
        self.assertIn("deletable: owned || (!employee && includeManaged) || Boolean(item.deletable)", javascript)
        self.assertIn('checkedCall("save_production_preset"', javascript)
        self.assertIn("recipe: { production_settings: portableProductionSettings(settings) }", javascript)
        self.assertIn("制作台现在可以直接选择", javascript)
        self.assertIn("管理员删除", javascript)
        self.assertIn('<b>制作方案 <em>${status}</em></b>', javascript)
        self.assertIn('id: "team-default"', javascript)
        self.assertIn('name: "团队默认"', javascript)
        self.assertNotIn('"员工制作方案"', javascript)

        self.assertIn("保存到制作台", html)
        self.assertIn("管理已保存方案", html)
        self.assertNotIn('<details class="style-free-editor" open', html)
        self.assertIn('<details class="panel setting-block settings-admin-only settings-defaults-fold">', html)
        self.assertNotIn('<details class="panel setting-block settings-admin-only settings-defaults-fold" open', html)
        self.assertIn("其他团队默认", html)
        for control_id in ("setting-chapter-pause", "setting-wpm", "setting-bgm-volume", "render-mode", "output-fps"):
            self.assertEqual(html.count(f'id="{control_id}"'), 1)
        self.assertIn("grid-template-columns: repeat(5,minmax(0,1fr))", css)
        self.assertIn(".settings-defaults-fold > summary", css)
        self.assertIn(".settings-defaults-content { display: grid; grid-template-columns: repeat(2,minmax(0,1fr))", css)
        self.assertIn("body.web-runtime .preview-settings { top: 56px", css)
        self.assertIn("align-self: start; max-height: calc(100vh - 36px)", css)

    def test_single_word_caption_diy_and_batch_override_are_available_to_employees(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('value="single">单词逐个出现', javascript)
        for control_id in (
            "subtitle-word-sync",
            "subtitle-unread-color",
            "subtitle-active-color",
            "subtitle-read-color",
            "subtitle-pop-intensity",
            "subtitle-pop-scale",
            "subtitle-pop-duration",
        ):
            self.assertIn(f'id="{control_id}"', html)
        self.assertIn('class="caption-word is-current"', html)
        self.assertIn("在真实配音句段时长内", html)
        for field in (
            "word_sync_enabled",
            "unread_color",
            "active_color",
            "read_color",
            "pop_scale",
            "pop_duration_ms",
            "pop_intensity",
        ):
            self.assertIn(f"{field}:", javascript)
        self.assertIn(".caption-word.is-current", css)
        self.assertIn("data-open-batch-style-studio", javascript)
        self.assertIn("普通员工也可以自由修改", javascript)
        self.assertIn("function applyVisualStyleToCurrentBatch", javascript)
        self.assertIn('id="production-intro-card-preset"', javascript)
        self.assertIn('id="production-code-card-preset"', javascript)
        self.assertIn('id="production-outro-card-preset"', javascript)

    def test_cover_story_dark_preview_uses_real_cover_brand_code_and_two_copy_regions(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('value="cover_story_dark"', html + javascript)
        self.assertIn('data-style-preset-value="cover_story_dark"', html)
        self.assertIn("cover_story_dark:", javascript)
        self.assertIn("preset-cover_story_dark", css)

        for element_id in (
            "preview-story-platform-mark",
            "preview-story-search",
            "preview-story-cover",
            "preview-story-copy-primary",
            "preview-story-copy-secondary",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('class="story-card-cover"', html)
        cover_start = html.index('id="preview-story-cover"')
        cover_end = html.index("</", cover_start)
        self.assertIn("<img", html[cover_start:cover_end])
        self.assertLess(
            html.index('id="preview-story-platform-mark"'),
            html.index('id="preview-story-copy-primary"'),
        )
        self.assertLess(
            html.index('id="preview-story-search"'),
            html.index('id="preview-story-copy-primary"'),
        )
        self.assertLess(
            html.index('id="preview-story-copy-primary"'),
            html.index('id="preview-story-cover"'),
        )
        self.assertLess(
            html.index('id="preview-story-cover"'),
            html.index('id="preview-story-copy-secondary"'),
        )
        self.assertIn("function paintIntroCardCover(", javascript)
        self.assertIn('applyStylePreset(styleCategoryForControl(control.id), control.value)', javascript)
        self.assertIn('paintIntroCardCover($("#preview-story-cover"), novel, introCardEnabled)', javascript)
        self.assertIn("function splitIntroPreviewCopy(", javascript)
        self.assertIn("function previewCardSentenceBoundaries(", javascript)
        self.assertIn("compact.slice(0, boundary.start)", javascript)
        self.assertIn("compact.slice(boundary.end)", javascript)
        self.assertIn('$("#preview-story-copy-primary")', javascript)
        self.assertIn('$("#preview-story-copy-secondary")', javascript)
        self.assertIn("root.dataset.introCopySize =", javascript)
        for copy_size in ("short", "medium", "long"):
            self.assertIn(f'[data-intro-copy-size="{copy_size}"]', css)
        self.assertIn(".story-card-cover > img", css)
        self.assertIn("object-fit: contain", css)
        self.assertIn("aspect-ratio:", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn(".story-card-cover { display: none", css)
        self.assertIn('[data-intro-preset="cover_story_dark"] .story-card-cover', css)
        self.assertIn('[data-intro-preset="cover_story_noir"] .story-card-cover', css)

        # Preview geometry follows the renderer's 1080x1920 safe-area
        # contract. Both approved cards reserve one right-hand real-cover
        # column across both left-hand synopsis regions.
        for contract_fragment in (
            "referenceWidth: 1080",
            "referenceHeight: 1920",
            "safeHorizontal: 76",
            "safeTop: 150",
            "safeBottom: 360",
            "coverAspect: 1.38",
            "Math.round(previewCardTextWidth(compact) * 0.40)",
            "const coverScale = { short: 0.72, medium: 0.86, long: 1 }[splitCopy.size]",
            "const coverWidth = coverPresent",
            "const coverGap = coverPresent ? contentGap : 0",
            "function resolveCoverSplitPreviewGeometry(",
            '"--cover-primary-left"',
            '"--cover-secondary-left"',
            '"--cover-image-height"',
            '"--cover-code-left"',
        ):
            self.assertIn(contract_fragment, javascript)
        self.assertIn("const coverFootprintTop = upperY + Math.max(0", javascript)
        self.assertIn("const codeChipX = panelX + panelWidth - padding - codeChipWidth", javascript)
        self.assertIn("const codeCopy = coverSplitCodeCopy(code.preview_value)", javascript)
        self.assertIn('"--cover-code-font-size"', javascript)
        self.assertIn('"--cover-code-padding"', javascript)
        self.assertIn("font-size: var(--cover-code-font-size", css)
        self.assertIn("padding: 0 var(--cover-code-padding", css)
        self.assertIn("const rotationDegrees = noirLayout ? -5 : 0", javascript)
        self.assertIn("left: var(--cover-secondary-left)", css)
        self.assertIn("height: var(--cover-image-height)", css)
        self.assertIn("transform: rotate(var(--cover-image-rotation))", css)
        self.assertIn('[data-intro-preset="cover_story_dark"] .story-card-cover { transform: rotate(0deg)', css)
        self.assertNotIn("grid-area: 2 / 1 / 3 / 3", css)
        self.assertNotIn('[data-intro-preset="cover_story_dark"][data-intro-copy-size=', css)
        self.assertNotIn('[data-intro-preset="cover_story_noir"][data-intro-copy-size=', css)
        self.assertNotIn(".story-summary-card:not(.has-story-cover)::after", css)

        self.assertIn("root.dataset.introCardEnabled = String(introCardEnabled)", javascript)
        self.assertIn('[data-intro-card-enabled="false"] .story-card-cover', css)
        self.assertIn('[data-intro-card-enabled="false"] .story-template-intro', css)

        # The selected layout remains fully editable rather than hard-coding a
        # single colour treatment into the preset implementation.
        for control_id in (
            "intro-headline-color",
            "intro-body-color",
            "intro-label-color",
            "intro-background",
            "intro-border",
        ):
            self.assertIn(f'id="{control_id}"', html)

    def test_retired_subtitle_presets_are_migration_only(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")
        models = (ROOT / "storyforge" / "models.py").read_text(encoding="utf-8")

        for retired_id in ("word_pop_sync", "minimal_bottom"):
            self.assertNotIn(f'value="{retired_id}"', html)
            self.assertNotIn(f'data-style-preset-value="{retired_id}"', html)
            self.assertNotIn(f"preset-{retired_id}", css)
            self.assertNotIn(retired_id, javascript)

        migration_start = models.index("RETIRED_SUBTITLE_PRESET_MIGRATIONS")
        migration_end = models.index(
            "# Presets are complete style patches", migration_start
        )
        migration_block = models[migration_start:migration_end]
        active_models = models[:migration_start] + models[migration_end:]
        for retired_id in ("word_pop_sync", "minimal_bottom"):
            self.assertIn(retired_id, migration_block)
            self.assertNotIn(retired_id, active_models)

        self.assertIn("normalize_retired_subtitle_settings", migration_block)
        self.assertIn('if (key === retiredBottomEffect) return "clear_outline"', javascript)
        self.assertIn('value="single">单词逐个出现', javascript)
        self.assertIn('wordMode === "single"', javascript)

    def test_lan_update_center_supports_host_publish_and_safe_client_apply(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        for control_id in (
            "update-center-title",
            "update-current-version",
            "update-available-version",
            "update-checked-at",
            "auto-update-enabled",
            "auto-download-updates",
            "update-check-minutes",
            "check-for-updates",
            "download-update",
            "schedule-update",
            "cancel-scheduled-update",
            "update-package-path",
            "update-publish-version",
            "update-release-notes",
            "publish-update",
            "clear-published-update",
            "open-employee-update",
            "employee-update-dialog",
            "employee-update-current-version",
            "employee-update-available-version",
            "employee-check-for-updates",
            "employee-download-update",
            "employee-schedule-update",
            "employee-restart-update",
            "employee-cancel-scheduled-update",
        ):
            self.assertIn(f'id="{control_id}"', html)
        self.assertIn("不会中断正在渲染的视频", html)
        self.assertIn("重启 StoryForge 时应用", html)
        for method in (
            "get_update_status",
            "check_for_updates",
            "download_update",
            "schedule_update_on_restart",
            "restart_to_apply_update",
            "cancel_scheduled_update",
            "publish_update",
            "clear_published_update",
        ):
            self.assertIn(f'"{method}"', javascript)
            self.assertIn(f'method === "{method}"', javascript)
        for setting in (
            "auto_update_enabled",
            "auto_download_updates",
            "update_check_minutes",
        ):
            self.assertIn(f"{setting}:", javascript)
        self.assertIn("function renderUpdateStatus", javascript)
        self.assertIn("function renderEmployeeUpdateStatus", javascript)
        self.assertIn("function openEmployeeUpdateDialog", javascript)
        self.assertIn(".update-center", css)
        self.assertIn(".update-state-badge", css)
        self.assertIn("body.web-runtime:not(.web-auth-required) .employee-update-trigger", css)
        self.assertIn(".employee-update-dialog", css)
        self.assertIn('webRequest("/web/api/update")', javascript)
        self.assertIn('window.location.assign(downloadUrl)', javascript)
        self.assertIn("不会自动开通付费服务或产生扣费", html)
        self.assertIn("默认开启自动检查与后台下载", html)
        self.assertIn('code === "client_update_required"', javascript)
        self.assertIn("Number(response?.status || payload?.status || 0) === 426", javascript)
        self.assertIn('required: "必须更新"', javascript)
        self.assertIn("当前版本过旧，不能领取新的制作任务", javascript)

    def test_authenticated_browser_runtime_preserves_desktop_and_supports_web_files(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        for element_id in (
            "web-login",
            "web-login-form",
            "web-login-username",
            "web-login-password",
            "web-session-bar",
            "web-session-name",
            "web-change-password",
            "web-logout",
            "web-password-dialog",
            "web-file-picker",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("当前密码", html)
        self.assertNotIn("密码或网页访问令牌", html)
        self.assertIn('minlength="8"', html)
        self.assertIn('maxlength="8"', html)
        self.assertIn('pattern="[!-~]{8}"', html)
        self.assertIn("恰好 8 位，可使用字母、数字和标点", html)
        self.assertIn("网页关闭不会中断", html)
        self.assertIn("function hasDesktopBridge()", javascript)
        self.assertIn(
            'typeof api.desktop_session_status === "function"', javascript
        )
        self.assertIn(
            "const api = hasDesktopBridge() ? window.pywebview.api : null;",
            javascript,
        )
        self.assertIn(
            "if (hasDesktopBridge()) initializeDesktopRuntime();", javascript
        )
        self.assertIn(
            "else if (isWebRuntime && !state.bootstrapped) initializeWebRuntime();",
            javascript,
        )
        self.assertIn('webRequest("/web/api/session/login"', javascript)
        self.assertIn('bridge.call("desktop_session_status")', javascript)
        self.assertIn('bridge.call("desktop_login", username, password)', javascript)
        self.assertIn('bridge.call("desktop_logout")', javascript)
        self.assertIn('/^[!-~]{8}$/.test(newPassword)', javascript)
        self.assertIn('webRequest("/web/api/session/password"', javascript)
        self.assertIn('webRequest("/web/api/rpc"', javascript)
        self.assertIn('/web/api/upload?kind=${encodeURIComponent(kind)}', javascript)
        self.assertIn('headers["X-StoryForge-CSRF"]', javascript)
        self.assertIn('credentials: "same-origin"', javascript)
        self.assertIn('new URLSearchParams(window.location.search).get("demo") === "1"', javascript)
        self.assertIn('method === "choose_folder"', javascript)
        self.assertIn("由当前制作电脑自行选择本机文件夹", javascript)
        self.assertIn("web_default_folders", javascript)
        self.assertNotIn("function webAllowedFolderRoots", javascript)
        self.assertNotIn("function approvedWebFolderOptions", javascript)
        self.assertIn("function draftFolderFieldMarkup", javascript)
        self.assertIn("文件夹只保存在当前制作电脑，不会同步到 Hub 主机", javascript)
        self.assertNotIn("启用本机制作服务.cmd", javascript)
        self.assertIn("网页端与软件使用同一套制作能力", html)
        self.assertIn('id="run-worker-self-check"', html)
        self.assertIn('id="worker-health-technical"', html)
        self.assertNotIn("轻量 EXE", html)
        self.assertNotIn("请在输入框中填写 StoryForge Hub 主机上的完整路径", javascript)
        self.assertIn("function webAssetUrl", javascript)
        self.assertIn("function artifactMediaSource", javascript)
        self.assertIn('"web-can-library-edit"', javascript)
        self.assertIn('"web-can-global-queue"', javascript)
        self.assertIn(".web-login-card", css)
        self.assertIn(".web-session-bar", css)
        self.assertIn(".artifact-download", css)
        self.assertIn(".hub-folder-panel", css)
        self.assertIn(".draft-folder-select-field", css)
        self.assertIn("body.web-runtime:not(.web-can-library-edit)", css)

    def test_employee_local_maintenance_is_separate_from_admin_provider_settings(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="provider-layout"', html)
        self.assertIn('id="employee-local-maintenance"', html)
        self.assertIn('id="employee-local-tts-provider"', html)
        self.assertIn('id="open-local-maintenance-update"', html)
        self.assertIn("provider-admin-config", html)
        self.assertIn("provider-local-maintenance-card", html)
        self.assertIn("function applyProviderAccessMode()", javascript)
        self.assertIn('checkedCall("set_local_tts_provider", selected)', javascript)
        self.assertIn('checkedCall("get_local_self_check")', javascript)
        self.assertIn('$("#save-providers")?.classList.toggle("is-hidden", employee)', javascript)
        self.assertIn(".provider-layout.is-employee-maintenance .provider-admin-config", css)
        self.assertIn(".provider-layout.is-employee-maintenance .provider-employee-local", css)

    def test_employee_hub_login_hides_machine_details_in_advanced_settings(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("员工只需账号密码", html)
        self.assertIn("登录这台制作电脑", html)
        self.assertIn("登录并开始制作", html)
        self.assertIn('<details class="hub-connection-advanced"', html)
        self.assertIn("连接设置（高级）", html)
        advanced_start = html.index('<details class="hub-connection-advanced"')
        advanced_end = html.index("</details>", advanced_start)
        advanced_markup = html[advanced_start:advanced_end]
        self.assertIn('id="hub-endpoint"', advanced_markup)
        self.assertIn('id="hub-device-name"', advanced_markup)
        self.assertNotIn('id="hub-access-token"', advanced_markup)
        self.assertIn("|| state.settings?.hub?.endpoint", javascript)
        self.assertIn("|| state.settings?.hub?.device_name", javascript)
        self.assertIn("请输入员工账号和密码。", javascript)
        self.assertIn("本机制作服务已自动启用", javascript)
        self.assertIn('item?.code === "worker_autostart_failed"', javascript)
        self.assertIn("showWorkerAutostartNotice(result.data)", javascript)
        self.assertIn("showWorkerAutostartNotice(data)", javascript)
        self.assertIn('.hub-settings-card[data-mode="client"] .hub-admin-machine-fields', css)
        self.assertIn(".hub-connection-advanced", css)

    def test_job_polling_is_incremental_and_recovers_after_network_errors(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="queue-sync-status"', html)
        self.assertIn("function jobsVisualSignature(jobs)", javascript)
        self.assertIn("const jobsChanged = nextJobsSignature !== state.jobVisualSignature", javascript)
        self.assertIn("scheduleJobPoll(retryDelay, pollEpoch)", javascript)
        self.assertIn("Math.min(15000", javascript)
        self.assertNotIn("window.setInterval(pollJobs, 1200)", javascript)
        self.assertIn('document.body.classList.toggle("production-resource-busy"', javascript)
        self.assertIn("body.production-resource-busy .video-preview", css)

    def test_job_polling_epoch_prevents_stale_session_writes(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        self.assertIn("pollEpoch: 0", javascript)
        self.assertIn("async function pollJobs(pollEpoch = state.pollEpoch)", javascript)
        self.assertIn(
            "if (!state.pollEnabled || pollEpoch !== state.pollEpoch) return;",
            javascript,
        )
        stop_start = javascript.index("  function stopPolling()")
        stop_end = javascript.index("\n  function bindEvents()", stop_start)
        stop_body = javascript[stop_start:stop_end]
        self.assertIn("state.pollEpoch += 1", stop_body)
        self.assertNotIn("state.pollInFlight = false", stop_body)
        self.assertIn(
            "if (state.pollEnabled && pollEpoch !== state.pollEpoch && !state.pollTimer)",
            javascript,
        )

    def test_visible_production_records_refresh_with_throttle_and_terminal_priority(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

        self.assertIn("const RECORD_POLL_INTERVAL_MS = 3000", javascript)
        self.assertIn("function scheduleProductionRecordRefresh", javascript)
        self.assertIn("function runScheduledProductionRecordRefresh", javascript)
        self.assertIn(
            "if (before !== job.status && terminalStatuses.has(job.status))",
            javascript,
        )
        self.assertIn(
            "scheduleProductionRecordRefresh({ urgent: recordRefreshNeeded });",
            javascript,
        )
        self.assertIn("expectedPollEpoch !== state.pollEpoch", javascript)

    def test_production_records_offer_authoritative_manual_refresh(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="record-refresh"', html)
        self.assertIn('id="record-refresh-proof" role="status" aria-live="polite"', html)
        self.assertIn("async function refreshProductionRecords(button)", javascript)
        self.assertIn("resetProductionRecordFilters();", javascript)
        self.assertIn("syncProductionRecordCacheFromGroups(groups);", javascript)
        self.assertIn("state.recordRefreshEpoch += 1", javascript)
        self.assertIn("const requestId = ++state.recordLoadRequestId;", javascript)
        self.assertIn("requestId !== state.recordLoadRequestId", javascript)
        self.assertIn(
            "expectedRecordRefreshEpoch !== state.recordRefreshEpoch",
            javascript,
        )
        self.assertIn("if (state.recordManualRefreshInFlight) return;", javascript)
        self.assertIn(
            '$("#record-refresh")?.addEventListener("click"',
            javascript,
        )
        self.assertIn(".record-refresh-actions", css)
        self.assertIn(".record-refresh-button", css)

    def test_hub_backup_ui_explains_deduplication_and_three_copy_limit(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="hub-backup-center"', html)
        self.assertIn("内容没变化就不重复备份", html)
        self.assertIn('id="create-hub-backup"', html)
        self.assertIn('id="refresh-hub-backups"', html)
        self.assertIn('id="hub-backup-list"', html)
        self.assertIn('checkedCall("list_hub_backups")', javascript)
        self.assertIn('checkedCall("create_hub_backup")', javascript)
        self.assertIn("data?.snapshot?.deduplicated", javascript)
        self.assertIn("function renderHubBackups()", javascript)
        self.assertIn(".hub-backup-center", css)

    def test_busy_preview_pauses_pseudo_element_animation(self) -> None:
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn(
            "body.production-resource-busy .video-preview .preview-media::before",
            css,
        )

    def test_stable_foundation_uses_seven_real_production_stages(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        stages = (
            ("text", "文本"),
            ("voice", "配音"),
            ("subtitle", "字幕"),
            ("preflight", "素材预检"),
            ("render", "渲染"),
            ("quality", "质检"),
            ("publish", "发布"),
        )
        for stage, label in stages:
            self.assertIn(f'data-stage="{stage}"', html)
            self.assertIn(f'<b>{label}</b>', html)
            self.assertIn(f'data-production-stage-status="{stage}"', html)
        self.assertIn("const productionStageCatalog = [", javascript)
        self.assertIn("function productionStageForJob(job)", javascript)
        self.assertIn("productionStageCatalog.findIndex", javascript)
        self.assertIn('node.classList.toggle("is-failed", failedHere)', javascript)
        self.assertIn('data-production-quick-jump="content"', html)
        self.assertIn('event.target.closest("[data-production-quick-jump]")', javascript)
        self.assertIn("missing?.selector || \"\"", javascript)
        self.assertIn("grid-template-columns: repeat(7, minmax(0, 1fr))", css)
        self.assertIn(".production-section-jumpbar [data-stage].is-current", css)
        self.assertIn(".production-preview-drawer {", css)
        self.assertIn("max-height: calc(100vh - 96px)", css)

    def test_failed_job_cards_explain_stage_reason_fix_and_available_actions(self) -> None:
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("function jobFailureCardMarkup(job, retryAction", javascript)
        self.assertIn("失败阶段 ·", javascript)
        self.assertIn("修复建议", javascript)
        self.assertIn("compactJobFailureReason(job)", javascript)
        self.assertIn("jobFailureFix(job)", javascript)
        self.assertIn("const logAction = safeLog", javascript)
        self.assertIn('const diagnosticAction = $("#run-worker-self-check")', javascript)
        self.assertIn('data-open-view="providers"', javascript)
        self.assertIn('data-retry-job="${escapeHtml(job.id)}"', javascript)
        self.assertIn("job.failure_diagnostics.log_tail", javascript)
        self.assertIn(".job-failure-card", css)
        self.assertIn(".job-support-detail > summary", css)
        self.assertIn(".job-support-button.is-primary", css)

    def test_employee_device_states_and_desktop_scaling_are_plain_language(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="employee-device-state"', html)
        for state_name, label in (
            ("ready", "可接任务"),
            ("busy", "制作中"),
            ("paused", "已暂停"),
            ("draining", "完成后暂停"),
            ("cooling", "冷却中"),
            ("degraded", "状态异常"),
            ("fault", "故障"),
        ):
            self.assertIn(f"{state_name}: {{", javascript)
            self.assertIn(f'label: "{label}"', javascript)
        self.assertIn("function employeeDeviceStateFor(device", javascript)
        self.assertIn("function currentEmployeeDeviceState()", javascript)
        self.assertIn("function renderEmployeeDeviceStateBadge", javascript)
        self.assertIn("discovered.health.state || discovered.health.health_state", javascript)
        self.assertIn("device.state", javascript)
        self.assertIn("device.governance?.state", javascript)
        self.assertIn("snapshot.state || state.localWorker?.healthState", javascript)
        self.assertIn("device.health_state", javascript)
        self.assertIn("metadata.health_state", javascript)
        self.assertIn("is-state-${escapeHtml(operationalState)}", javascript)
        self.assertIn(".employee-device-state.is-ready", css)
        self.assertIn(".managed-device-row.is-state-busy", css)
        self.assertIn(".managed-device-status .device-state-badge.is-fault", css)
        self.assertIn("clamp(214px, 15.6vw, 250px)", css)
        self.assertIn("clamp(324px, 23.5vw, 360px)", css)
        self.assertIn("@media (max-width: 1500px)", css)

        offline_guard = 'if (device.online === false) return "degraded";'
        stale_busy_guard = "if (queue.busy || device.busy || metadata.queue_busy || explicit === \"busy\") return \"busy\";"
        self.assertIn(offline_guard, javascript)
        self.assertLess(javascript.index(offline_guard), javascript.index(stale_busy_guard))

    def test_noninteractive_production_stages_use_status_elements_not_buttons(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertNotIn("<button type=\"button\" data-stage=\"render\"", html)
        self.assertNotIn("<button type=\"button\" data-stage=\"quality\"", html)
        self.assertNotIn("<button type=\"button\" data-stage=\"publish\"", html)
        for stage in ("render", "quality", "publish"):
            self.assertIn(
                f'<div class="production-stage-state" data-stage="{stage}" data-stage-only role="status"',
                html,
            )
        self.assertIn(".production-section-jumpbar [data-stage]", css)
        self.assertIn(".production-stage-state", css)

    def test_local_storage_tools_are_safe_and_available_from_browser_or_desktop(self) -> None:
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="scan-local-storage"', html)
        self.assertIn('id="cleanup-local-storage"', html)
        self.assertIn('id="create-support-bundle"', html)
        self.assertIn("小说源文件与最终成片不会删除", html)
        for method in (
            "get_local_storage_status",
            "cleanup_local_storage_cache",
            "create_local_support_bundle",
        ):
            self.assertIn(f'"{method}"', javascript)
        self.assertIn("confirm: false", javascript)
        self.assertIn("confirm: true", javascript)
        self.assertIn("window.confirm(", javascript)
        self.assertIn(".local-storage-tools", css)


if __name__ == "__main__":
    unittest.main()
