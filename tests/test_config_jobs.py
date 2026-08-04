from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from storyforge.api import StoryForgeApi
from storyforge.config import (
    MASKED_SECRET,
    SETTINGS_SCHEMA_VERSION,
    ApplicationState,
    SecretProtector,
    SettingsRepository,
)
from storyforge.jobs import JobQueue
from storyforge.models import (
    DEFAULT_PREVIEW_SECONDS,
    VISUAL_STYLE_PRESETS,
    AppSettings,
    BatchSpec,
    JobStatus,
    PlatformProfile,
    RenderJob,
)


class _PortableProtector:
    """Deterministic protector used to test repository wiring on every OS."""

    PREFIX = "test-sealed:"

    def protect(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith(self.PREFIX):
            return value
        return self.PREFIX + base64.b64encode(value.encode("utf-8")).decode("ascii")

    def unprotect(self, value: str) -> str:
        if not value or not value.startswith(self.PREFIX):
            return value
        return base64.b64decode(value[len(self.PREFIX) :]).decode("utf-8")


def _batch(root: Path, platform_id: str = "platform-1") -> BatchSpec:
    text = root / "texts"
    videos = root / "videos"
    music = root / "music"
    output = root / "output"
    for folder in (text, videos, music, output):
        folder.mkdir(parents=True, exist_ok=True)
    return BatchSpec(
        id="batch-1",
        platform_id=platform_id,
        text_folder=str(text),
        video_folder=str(videos),
        music_folder=str(music),
        output_folder=str(output),
    )


def _join_queue(test: unittest.TestCase, queue: JobQueue) -> None:
    worker = queue._worker
    test.assertIsNotNone(worker)
    assert worker is not None
    worker.join(timeout=5)
    test.assertFalse(worker.is_alive(), "render queue did not finish")


class SettingsRepositoryTests(unittest.TestCase):
    def test_retired_subtitle_presets_migrate_without_losing_visual_style(self) -> None:
        word_pop = AppSettings.from_dict(
            {
                "subtitle_preset": "word_pop_sync",
                "subtitle_word_mode": "off",
                "subtitle": {"active_color": "#12ABEF", "pop_scale": 126},
            }
        )
        minimal = AppSettings.from_dict(
            {
                "subtitle_preset": "minimal_bottom",
                "subtitle_word_mode": "off",
                "subtitle": {"bottom_margin": 275},
            }
        )

        self.assertEqual(word_pop.subtitle_preset, "clear_outline")
        self.assertEqual(word_pop.subtitle_word_mode, "single")
        self.assertEqual(word_pop.subtitle.font_size, 52)
        self.assertEqual(word_pop.subtitle.active_color, "#12ABEF")
        self.assertEqual(word_pop.subtitle.pop_scale, 126)
        self.assertFalse(word_pop.subtitle.word_sync_enabled)
        self.assertEqual(minimal.subtitle_preset, "clear_outline")
        self.assertEqual(minimal.subtitle_word_mode, "off")
        self.assertEqual(minimal.subtitle.font_family, "Segoe UI")
        self.assertEqual(minimal.subtitle.bottom_margin, 275)
        self.assertNotIn("word_pop_sync", VISUAL_STYLE_PRESETS["subtitle"])
        self.assertNotIn("minimal_bottom", VISUAL_STYLE_PRESETS["subtitle"])

        round_trip = AppSettings.from_dict(word_pop.to_dict())
        self.assertEqual(round_trip.to_dict(), word_pop.to_dict())

    def test_schema_nineteen_settings_are_rewritten_with_retired_preset_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            repository.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 19,
                        "settings": {
                            "subtitle_preset": "word_pop_sync",
                            "subtitle_word_mode": "off",
                            "subtitle": {"active_color": "#12ABEF"},
                        },
                        "platforms": [],
                        "batches": [],
                    }
                ),
                encoding="utf-8",
            )

            migrated, _, _ = repository.load()
            persisted = json.loads(
                repository.settings_path.read_text(encoding="utf-8")
            )

        self.assertGreater(SETTINGS_SCHEMA_VERSION, 19)
        self.assertEqual(migrated.subtitle_preset, "clear_outline")
        self.assertEqual(migrated.subtitle_word_mode, "single")
        self.assertEqual(persisted["schema_version"], SETTINGS_SCHEMA_VERSION)
        self.assertEqual(persisted["settings"]["subtitle_preset"], "clear_outline")
        self.assertEqual(persisted["settings"]["subtitle_word_mode"], "single")
        self.assertEqual(persisted["settings"]["subtitle"]["active_color"], "#12ABEF")

    def test_recommended_narration_default_is_240_wpm(self) -> None:
        self.assertEqual(AppSettings().narration_wpm, 240)

    def test_schema_17_legacy_false_keeps_video_intent_as_complete_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SettingsRepository(root / "state")
            legacy_batch = _batch(root).to_dict()
            legacy_batch.pop("output_mode", None)
            legacy_batch["export_narration_audio"] = False
            repository.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 17,
                        "settings": {},
                        "platforms": [],
                        "batches": [legacy_batch],
                    }
                ),
                encoding="utf-8",
            )

            _settings, _platforms, batches = repository.load()

            self.assertEqual(len(batches), 1)
            self.assertEqual(batches[0].output_mode, "video_and_mp3")
            persisted = json.loads(
                repository.settings_path.read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["schema_version"], SETTINGS_SCHEMA_VERSION)
            self.assertEqual(
                persisted["batches"][0]["output_mode"],
                "video_and_mp3",
            )

    def test_hub_artifact_sharing_is_permanently_disabled_and_legacy_true_is_reset(self) -> None:
        self.assertFalse(AppSettings().hub.share_previews)
        self.assertFalse(AppSettings().hub.share_narration)
        self.assertFalse(AppSettings.from_dict({}).hub.share_narration)
        forged = AppSettings.from_dict(
            {"hub": {"share_previews": True, "share_narration": True}}
        )
        self.assertFalse(forged.hub.share_previews)
        self.assertFalse(forged.hub.share_narration)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = SettingsRepository(root / "missing")
            missing.settings_path.parent.mkdir(parents=True, exist_ok=True)
            missing.settings_path.write_text(
                json.dumps({"schema_version": 16, "settings": {"hub": {}}}),
                encoding="utf-8",
            )
            migrated, _, _ = missing.load()
            self.assertFalse(migrated.hub.share_narration)

            explicit = SettingsRepository(root / "explicit")
            explicit.settings_path.parent.mkdir(parents=True, exist_ok=True)
            explicit.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 16,
                        "settings": {
                            "hub": {
                                "share_previews": True,
                                "share_narration": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            migrated_explicit, _, _ = explicit.load()
            self.assertFalse(migrated_explicit.hub.share_previews)
            self.assertFalse(migrated_explicit.hub.share_narration)

            current = SettingsRepository(root / "current")
            current.settings_path.parent.mkdir(parents=True, exist_ok=True)
            current.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 17,
                        "settings": {
                            "hub": {
                                "share_previews": True,
                                "share_narration": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            preserved_current, _, _ = current.load()
            self.assertFalse(preserved_current.hub.share_previews)
            self.assertFalse(preserved_current.hub.share_narration)
            persisted = json.loads(
                current.settings_path.read_text(encoding="utf-8")
            )["settings"]["hub"]
            self.assertFalse(persisted["share_previews"])
            self.assertFalse(persisted["share_narration"])

    def test_missing_installation_identity_is_generated_once_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            repository.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": SETTINGS_SCHEMA_VERSION,
                        "settings": {"hub": {}},
                        "platforms": [],
                        "batches": [],
                    }
                ),
                encoding="utf-8",
            )

            first, _platforms, _batches = repository.load()
            second, _platforms, _batches = repository.load()

            self.assertEqual(first.hub.installation_id, second.hub.installation_id)
            persisted = json.loads(
                repository.settings_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                persisted["settings"]["hub"]["installation_id"],
                first.hub.installation_id,
            )

    def test_settings_platforms_batches_and_secrets_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SettingsRepository(root / "state")
            repository._protector = _PortableProtector()
            settings = AppSettings(
                adult_mode="direct",
                narration_wpm=240,
                output_mode="reuse_audio",
                video_playback_speed=2.25,
                video_transition="fade",
                subtitle_word_mode="single",
                bgm_mode="none",
                video_template="platform_story_card",
                export_narration_audio=True,
                voice_by_mood={"suspense": "af_nicole"},
            )
            settings.subtitle.font_size = 61
            settings.providers.text_provider = "groq"
            settings.providers.text_api_key = "text-secret-value"
            settings.providers.tts_provider = "deepgram"
            settings.providers.tts_api_key = "voice-secret-value"
            settings.hub.mode = "client"
            settings.hub.endpoint = "http://192.168.1.20:8765"
            settings.hub.account_username = "renderer-one"
            settings.hub.access_token = "sfh-device-secret-value"
            platform = PlatformProfile(
                id="platform-1",
                name="NovelBox",
                search_template="Search {platform}: {code}",
                logo_path=str(root / "novelbox-logo.png"),
                brand_color="#E94B5F",
            )
            batch = _batch(root)
            batch.export_narration_audio = True

            repository.save(settings, [platform], [batch])

            raw_text = repository.settings_path.read_text(encoding="utf-8")
            self.assertEqual(
                json.loads(raw_text)["schema_version"], SETTINGS_SCHEMA_VERSION
            )
            self.assertNotIn("text-secret-value", raw_text)
            self.assertNotIn("voice-secret-value", raw_text)
            self.assertNotIn("sfh-device-secret-value", raw_text)
            self.assertIn(_PortableProtector.PREFIX, raw_text)

            reloaded = SettingsRepository(root / "state")
            reloaded._protector = _PortableProtector()
            loaded_settings, platforms, batches = reloaded.load()
            self.assertEqual(loaded_settings.adult_mode, "direct")
            self.assertEqual(loaded_settings.narration_wpm, 240)
            self.assertEqual(loaded_settings.output_mode, "reuse_audio")
            self.assertEqual(loaded_settings.video_playback_speed, 2.25)
            self.assertEqual(loaded_settings.video_transition, "fade")
            self.assertEqual(loaded_settings.subtitle_word_mode, "single")
            self.assertEqual(loaded_settings.bgm_mode, "none")
            self.assertEqual(loaded_settings.video_template, "platform_story_card")
            self.assertTrue(loaded_settings.export_narration_audio)
            self.assertEqual(loaded_settings.subtitle.font_size, 61)
            self.assertEqual(loaded_settings.providers.text_api_key, "text-secret-value")
            self.assertEqual(loaded_settings.providers.tts_api_key, "voice-secret-value")
            self.assertEqual(loaded_settings.hub.account_username, "renderer-one")
            self.assertEqual(loaded_settings.hub.access_token, "sfh-device-secret-value")
            self.assertEqual(platforms, [platform])
            self.assertEqual(batches, [batch])

    def test_narration_audio_export_fields_are_serializable_and_legacy_safe(self) -> None:
        self.assertFalse(AppSettings().export_narration_audio)
        self.assertFalse(AppSettings.from_dict({}).export_narration_audio)
        self.assertTrue(
            AppSettings.from_dict(
                {"export_narration_audio": True}
            ).export_narration_audio
        )
        self.assertFalse(
            AppSettings.from_dict(
                {"export_narration_audio": "false"}
            ).export_narration_audio
        )

        job = RenderJob(
            batch_id="batch",
            platform_id="platform",
            source_file=__file__,
            title="Story",
            code="CODE-1",
            video_folder="videos",
            music_folder="music",
            output_folder="output",
            publish_batch_folder="output/待发布/NovelBox_CODE-1_Story_Bbatch001",
            narration_audio_file=(
                "output/待发布/NovelBox_CODE-1_Story_Bbatch001/"
                "001_NovelBox_CODE-1_E001_V01_Bbatch001.mp3"
            ),
        )
        restored = RenderJob.from_dict(job.to_dict())
        self.assertEqual(
            restored.narration_audio_file,
            "output/待发布/NovelBox_CODE-1_Story_Bbatch001/"
            "001_NovelBox_CODE-1_E001_V01_Bbatch001.mp3",
        )
        self.assertEqual(
            restored.publish_batch_folder,
            "output/待发布/NovelBox_CODE-1_Story_Bbatch001",
        )
        legacy_payload = job.to_dict()
        legacy_payload.pop("narration_audio_file")
        legacy_payload.pop("publish_batch_folder")
        restored_legacy = RenderJob.from_dict(legacy_payload)
        self.assertEqual(restored_legacy.narration_audio_file, "")
        self.assertEqual(restored_legacy.publish_batch_folder, "")

    def test_cover_outro_switch_is_serializable_and_legacy_safe(self) -> None:
        self.assertTrue(AppSettings().cover_outro_enabled)
        self.assertTrue(AppSettings.from_dict({}).cover_outro_enabled)
        self.assertFalse(
            AppSettings.from_dict(
                {"cover_outro_enabled": False}
            ).cover_outro_enabled
        )
        self.assertTrue(
            AppSettings.from_dict(
                {"cover_outro_enabled": "false"}
            ).cover_outro_enabled
        )

        job = RenderJob(
            batch_id="batch",
            platform_id="platform",
            source_file=__file__,
            title="Story",
            code="CODE-1",
            video_folder="videos",
            music_folder="music",
            output_folder="output",
            cover_outro_enabled=False,
        )
        self.assertFalse(RenderJob.from_dict(job.to_dict()).cover_outro_enabled)
        legacy_payload = job.to_dict()
        legacy_payload.pop("cover_outro_enabled")
        self.assertTrue(RenderJob.from_dict(legacy_payload).cover_outro_enabled)

    def test_render_job_batch_position_is_serializable_and_legacy_safe(self) -> None:
        job = RenderJob(
            batch_id="durable-batch",
            platform_id="platform",
            source_file=__file__,
            title="Story",
            code="CODE-1",
            video_folder="videos",
            music_folder="music",
            output_folder="output",
            batch_total_count=750,
            batch_ordinal=501,
        )
        restored = RenderJob.from_dict(job.to_dict())
        self.assertEqual(restored.batch_total_count, 750)
        self.assertEqual(restored.batch_ordinal, 501)
        legacy = job.to_dict()
        legacy.pop("batch_total_count")
        legacy.pop("batch_ordinal")
        restored_legacy = RenderJob.from_dict(legacy)
        self.assertEqual(restored_legacy.batch_total_count, 0)
        self.assertEqual(restored_legacy.batch_ordinal, 0)

    def test_legacy_platform_without_branding_loads_safe_empty_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            repository.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": SETTINGS_SCHEMA_VERSION,
                        "settings": {},
                        "platforms": [
                            {
                                "id": "legacy-platform",
                                "name": "Legacy Novel",
                                "search_template": "Search {platform}: {code}",
                                "ending_template": "Find {code} on {platform}.",
                            }
                        ],
                        "batches": [],
                    }
                ),
                encoding="utf-8",
            )

            _settings, platforms, _batches = repository.load()

            self.assertEqual(platforms[0].logo_path, "")
            self.assertEqual(platforms[0].brand_color, "")

    @unittest.skipUnless(os.name == "nt", "DPAPI is only available on Windows")
    def test_windows_dpapi_round_trip_is_user_scoped_and_not_plaintext(self) -> None:
        protector = SecretProtector()
        secret = "测试-key-123"
        protected = protector.protect(secret)
        self.assertTrue(protected.startswith(SecretProtector.PREFIX))
        self.assertNotIn(secret, protected)
        self.assertEqual(protector.unprotect(protected), secret)

    def test_corrupt_json_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            repository.settings_path.write_text("{not valid json", encoding="utf-8")
            settings, platforms, batches = repository.load()
            self.assertEqual(settings, AppSettings())
            self.assertEqual(platforms, [])
            self.assertEqual(batches, [])

    def test_video_template_defaults_and_legacy_schema_migration_are_classic(self) -> None:
        self.assertEqual(AppSettings().video_template, "classic")
        self.assertEqual(AppSettings.from_dict({}).video_template, "classic")
        self.assertEqual(AppSettings().intro_animation, "fade_rise")
        self.assertEqual(AppSettings().color_grade, "neutral")

        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            repository.settings_path.write_text(
                json.dumps({"schema_version": 6, "settings": {}}),
                encoding="utf-8",
            )

            settings, _, _ = repository.load()

            self.assertEqual(settings.video_template, "classic")
            migrated = json.loads(repository.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["schema_version"], SETTINGS_SCHEMA_VERSION)
            self.assertEqual(migrated["settings"]["video_template"], "classic")
            self.assertEqual(migrated["settings"]["intro_animation"], "fade_rise")
            self.assertEqual(migrated["settings"]["color_grade"], "neutral")

    def test_output_fps_defaults_to_60_migrates_once_and_keeps_30_as_an_option(self) -> None:
        self.assertEqual(AppSettings().output_fps, 60)
        self.assertEqual(AppSettings.from_dict({}).output_fps, 60)
        self.assertEqual(AppSettings.from_dict({"output_fps": 30}).output_fps, 30)
        self.assertEqual(AppSettings.from_dict({"output_fps": 24}).output_fps, 60)

        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            repository.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 7,
                        "settings": {"output_fps": 30},
                    }
                ),
                encoding="utf-8",
            )

            migrated_settings, platforms, batches = repository.load()
            self.assertEqual(migrated_settings.output_fps, 60)
            migrated = json.loads(repository.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["schema_version"], SETTINGS_SCHEMA_VERSION)
            self.assertEqual(migrated["settings"]["output_fps"], 60)

            migrated_settings.output_fps = 30
            repository.save(migrated_settings, platforms, batches)
            reloaded, _, _ = repository.load()
            self.assertEqual(reloaded.output_fps, 30)

    def test_preview_defaults_to_fifteen_seconds_and_migrates_old_default(self) -> None:
        self.assertEqual(DEFAULT_PREVIEW_SECONDS, 15)
        self.assertEqual(AppSettings().preview_seconds, DEFAULT_PREVIEW_SECONDS)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = SettingsRepository(root / "legacy-preview")
            legacy.settings_path.parent.mkdir(parents=True, exist_ok=True)
            legacy.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 11,
                        "settings": {"preview_seconds": 30},
                    }
                ),
                encoding="utf-8",
            )

            migrated, _, _ = legacy.load()

            self.assertEqual(migrated.preview_seconds, DEFAULT_PREVIEW_SECONDS)
            saved = json.loads(legacy.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], SETTINGS_SCHEMA_VERSION)
            self.assertEqual(
                saved["settings"]["preview_seconds"],
                DEFAULT_PREVIEW_SECONDS,
            )

            custom = SettingsRepository(root / "custom-preview")
            custom.settings_path.parent.mkdir(parents=True, exist_ok=True)
            custom.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 11,
                        "settings": {"preview_seconds": 20},
                    }
                ),
                encoding="utf-8",
            )
            custom_settings, _, _ = custom.load()
            self.assertEqual(custom_settings.preview_seconds, 20)

    def test_usage_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            repository.save_usage({"clip-a.mp4": 2, "clip-b.mp4": 0})
            self.assertEqual(
                repository.load_usage(), {"clip-a.mp4": 2, "clip-b.mp4": 0}
            )

    def test_legacy_wpm_is_migrated_into_supported_range_and_valid_custom_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SettingsRepository(root / "legacy")
            repository.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "settings": {
                            "narration_wpm": 155,
                            "subtitle": {"max_chars_per_line": 34, "bottom_margin": 509},
                        },
                    }
                ),
                encoding="utf-8",
            )
            settings, _, _ = repository.load()
            self.assertEqual(settings.narration_wpm, 210)
            self.assertEqual(settings.subtitle.max_chars_per_line, 28)
            self.assertEqual(settings.subtitle.horizontal_margin, 180)
            self.assertEqual(settings.subtitle.bottom_margin, 509)
            migrated = json.loads(repository.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["schema_version"], SETTINGS_SCHEMA_VERSION)

            custom = SettingsRepository(root / "custom")
            custom.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "settings": {
                            "narration_wpm": 240,
                            "subtitle": {"max_chars_per_line": 30},
                        },
                    }
                ),
                encoding="utf-8",
            )
            custom_settings, _, _ = custom.load()
            self.assertEqual(custom_settings.narration_wpm, 240)
            self.assertEqual(custom_settings.subtitle.max_chars_per_line, 30)

            version_two = SettingsRepository(root / "version-two")
            version_two.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "settings": {"narration_wpm": 180},
                    }
                ),
                encoding="utf-8",
            )
            version_two_settings, _, _ = version_two.load()
            self.assertEqual(version_two_settings.narration_wpm, 210)

            version_two_custom = SettingsRepository(root / "version-two-custom")
            version_two_custom.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "settings": {"narration_wpm": 195},
                    }
                ),
                encoding="utf-8",
            )
            version_two_custom_settings, _, _ = version_two_custom.load()
            self.assertEqual(version_two_custom_settings.narration_wpm, 240)

            version_three = SettingsRepository(root / "version-three")
            version_three.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "settings": {"narration_wpm": 210, "bgm_volume": 0.16},
                    }
                ),
                encoding="utf-8",
            )
            version_three_settings, _, _ = version_three.load()
            self.assertEqual(version_three_settings.bgm_volume, 0.28)

            version_three_custom = SettingsRepository(root / "version-three-custom")
            version_three_custom.settings_path.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "settings": {"narration_wpm": 210, "bgm_volume": 0.22},
                    }
                ),
                encoding="utf-8",
            )
            version_three_custom_settings, _, _ = version_three_custom.load()
            self.assertEqual(version_three_custom_settings.bgm_volume, 0.22)


class ApplicationStateAndApiTests(unittest.TestCase):
    def setUp(self) -> None:
        # These tests validate application state and API orchestration.  The
        # production disk/memory gate is covered separately and must not make
        # this suite depend on the current developer machine's free space.
        admission_patch = mock.patch(
            "storyforge.worker.default_heavy_job_admission",
            return_value={"allowed": True, "reason": "", "message": ""},
        )
        admission_patch.start()
        self.addCleanup(admission_patch.stop)

    def test_settings_api_cannot_enable_hub_production_media_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))
            try:
                saved = api.save_settings(
                    {
                        "hub": {
                            "share_previews": True,
                            "share_narration": True,
                        }
                    }
                )
                self.assertTrue(saved["ok"], saved)
                self.assertFalse(saved["data"]["hub"]["share_previews"])
                self.assertFalse(saved["data"]["hub"]["share_narration"])

                persisted = json.loads(
                    repository.settings_path.read_text(encoding="utf-8")
                )["settings"]["hub"]
                self.assertFalse(persisted["share_previews"])
                self.assertFalse(persisted["share_narration"])
            finally:
                api._shutdown()

    def test_legacy_batch_output_flag_maps_explicitly_to_v04_modes(self) -> None:
        base = {
            "platform_id": "platform",
            "text_folder": r"C:\stories",
            "video_folder": "",
            "music_folder": "",
            "output_folder": r"C:\output",
        }
        current_video = StoryForgeApi._batch_from_payload(
            {**base, "output_mode": "video_and_mp3"}
        )
        self.assertEqual(current_video.output_mode, "video_and_mp3")
        self.assertFalse(current_video.export_narration_audio)
        self.assertEqual(
            StoryForgeApi._batch_from_payload(
                {**base, "output_mode": "audio_only"}
            ).output_mode,
            "audio_only",
        )
        reused = StoryForgeApi._batch_from_payload(
            {
                **base,
                "output_mode": "reuse_audio",
                "source_narration_audio": r"C:\audio\existing.mp3",
            }
        )
        self.assertEqual(reused.output_mode, "reuse_audio")
        self.assertEqual(
            reused.source_narration_audio,
            r"C:\audio\existing.mp3",
        )
        legacy_pair = StoryForgeApi._batch_from_payload(
            {**base, "export_narration_audio": True}
        )
        self.assertEqual(legacy_pair.output_mode, "video_and_mp3")
        self.assertTrue(legacy_pair.export_narration_audio)
        self.assertEqual(
            StoryForgeApi._batch_from_payload(
                {**base, "export_narration_audio": False}
            ).output_mode,
            "video_and_mp3",
        )
        self.assertEqual(
            StoryForgeApi._batch_from_payload(base).output_mode,
            "video_and_mp3",
        )

    def test_audio_only_completion_records_narration_but_never_video_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SettingsRepository(root / "state")
            api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))
            audio = root / "001_Story.mp3"
            audio.write_bytes(b"mp3")
            job = RenderJob(
                id="audio-only-job",
                batch_id="audio-batch",
                platform_id="platform",
                source_file=str(root / "story.txt"),
                title="Story",
                code="AUDIO01",
                video_folder="",
                music_folder="",
                output_folder=str(root),
                output_file=str(audio),
                narration_audio_file=str(audio),
                production_record_id="record-audio",
                status=JobStatus.COMPLETED,
                progress=1.0,
                settings_snapshot={"output_mode": "audio_only"},
            )
            with (
                mock.patch.object(
                    api._catalog,
                    "save_production_record",
                    return_value={"id": "record-audio", "batch_id": "audio-batch"},
                ),
                mock.patch.object(api, "_record_media_from_manifest"),
                mock.patch.object(api, "_record_artifact") as record_artifact,
                mock.patch.object(api, "_release_record_lease"),
            ):
                api._sync_one_job_record(job)

            record_artifact.assert_called_once_with(
                job,
                "narration",
                str(audio),
            )

    def test_failed_job_syncs_only_sanitized_diagnostics_to_hub_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SettingsRepository(root / "state")
            api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))
            diagnostics = {
                "schema_version": 1,
                "code": "disk_full",
                "summary": "制作电脑的可用磁盘空间不足。",
                "stage": "ffmpeg_render",
                "log_name": "render-error.log",
                "log_tail": "No space left on device",
                "truncated": False,
                "captured_at": "2026-07-29T00:00:00+00:00",
            }
            job = RenderJob(
                id="failed-job",
                batch_id="failed-batch",
                platform_id="platform",
                source_file=str(root / "story.txt"),
                title="Story",
                code="B56826",
                video_folder=str(root),
                music_folder=str(root),
                output_folder=str(root),
                production_record_id="record-failed",
                status=JobStatus.FAILED,
                progress=0.68,
                message=(
                    r"PipelineError: FFmpeg 渲染失败 C:\Users\Admin\private\render-error.log"
                ),
                error_log=str(root / "private" / "render-error.log"),
                failure_diagnostics=diagnostics,
            )
            api._job_media_selection[job.id] = {
                "mode": "category",
                "source_root": r"E:\employee-media\romance",
                "fallback_reason": r"failed at E:\employee-media\romance\broken.mp4",
            }
            with (
                mock.patch.object(
                    api._catalog,
                    "save_production_record",
                    return_value={"id": "record-failed", "batch_id": "failed-batch"},
                ) as save_record,
                mock.patch.object(api, "_release_record_lease"),
            ):
                api._sync_one_job_record(job)

            payload = save_record.call_args.args[0]
            self.assertEqual(payload["metadata"]["failure_diagnostics"], diagnostics)
            self.assertNotIn(job.error_log, str(payload["metadata"]))
            self.assertNotIn(r"C:\Users\Admin", payload["error_message"])
            self.assertNotIn(
                r"E:\employee-media", str(payload["metadata"]["media_selection"])
            )
            self.assertIn(
                "<path>", str(payload["metadata"]["media_selection"])
            )

    def test_api_archives_and_restores_finished_job_with_durable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SettingsRepository(root / "state")
            queue = JobQueue(lambda *_: "")
            api = StoryForgeApi(repository=repository, queue=queue)
            saved_platform = api.save_platform({"name": "Archive Platform"})
            self.assertTrue(saved_platform["ok"], saved_platform)
            platform = api._state.platforms[0]
            imported = api._catalog.import_novel(
                {
                    "title": "Archive Story",
                    "body": "Chapter 1\nA finished story task can be archived safely.",
                    "episodes": [
                        {
                            "ordinal": 1,
                            "title": "Opening",
                            "source_map": [],
                            "estimated_duration_seconds": 10,
                        }
                    ],
                }
            )["novel"]
            binding = api._catalog.save_novel_binding(
                {"novel_id": imported["id"], "platform_name": platform.name}
            )
            code = api._catalog.add_promo_code(
                {"binding_id": binding["id"], "code": "ARC001"}
            )
            draft = api._catalog.save_draft(
                {
                    "novel_id": imported["id"],
                    "binding_id": binding["id"],
                    "promo_code_id": code["id"],
                    "creative_line_count": 1,
                    "episode_ids": [imported["current_revision"]["episodes"][0]["id"]],
                }
            )
            record = api._catalog.save_production_record(
                {"draft_id": draft["id"], "job_id": "archive-api-job", "status": "failed"}
            )
            job = RenderJob(
                id="archive-api-job",
                batch_id="batch",
                platform_id=platform.id,
                source_file=str(root / "story.txt"),
                title="Archive Story",
                code="ARC001",
                video_folder=str(root),
                music_folder=str(root),
                output_folder=str(root),
                status=JobStatus.FAILED,
                message="synthetic failure",
                production_record_id=record["id"],
            )
            queue.enqueue_jobs([job], platform)

            archived = api.archive_job(job.id)

            self.assertTrue(archived["ok"], archived)
            self.assertEqual(queue.list_jobs(), [])
            self.assertEqual(api.get_archived_jobs()["data"][0]["id"], job.id)
            self.assertEqual(
                api.get_archived_jobs()["data"][0]["batch_id"],
                record["batch_id"],
            )
            archived_page = api.get_archived_jobs({"limit": 1, "offset": 0})
            self.assertTrue(archived_page["ok"], archived_page)
            self.assertEqual(archived_page["data"]["total"], 1)
            self.assertEqual(archived_page["data"]["limit"], 1)
            self.assertEqual(archived_page["data"]["offset"], 0)
            self.assertEqual(archived_page["data"]["items"][0]["id"], job.id)
            bootstrap = api.get_bootstrap()["data"]
            self.assertEqual(bootstrap["archived_jobs_total"], 1)
            self.assertEqual(bootstrap["archived_jobs"][0]["id"], job.id)
            reopened = type(api._catalog)(api._catalog.database_path)
            self.assertEqual(reopened.get_archived_job(job.id)["message"], "synthetic failure")

            restored = api.restore_job(job.id)

            self.assertTrue(restored["ok"], restored)
            self.assertFalse(restored["data"]["job"]["archived"])
            self.assertEqual(restored["data"]["job"]["batch_id"], record["batch_id"])
            self.assertEqual(queue.list_jobs()[0]["batch_id"], record["batch_id"])
            self.assertEqual(queue.list_jobs()[0]["message"], "synthetic failure")
            self.assertEqual(api.get_archived_jobs()["data"], [])

    def test_api_archives_and_restores_a_complete_batch_without_partial_cards(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SettingsRepository(root / "state")
            queue = JobQueue(lambda *_: "")
            api = StoryForgeApi(repository=repository, queue=queue)
            saved_platform = api.save_platform({"name": "Batch Archive Platform"})
            self.assertTrue(saved_platform["ok"], saved_platform)
            platform = api._state.platforms[0]
            imported = api._catalog.import_novel(
                {
                    "title": "Batch Archive Story",
                    "body": "Chapter 1\nA complete batch should move together.",
                    "episodes": [
                        {
                            "ordinal": 1,
                            "title": "Opening",
                            "source_map": [],
                            "estimated_duration_seconds": 10,
                        }
                    ],
                }
            )["novel"]
            binding = api._catalog.save_novel_binding(
                {"novel_id": imported["id"], "platform_name": platform.name}
            )
            code = api._catalog.add_promo_code(
                {"binding_id": binding["id"], "code": "BAT001"}
            )
            draft = api._catalog.save_draft(
                {
                    "novel_id": imported["id"],
                    "binding_id": binding["id"],
                    "promo_code_id": code["id"],
                    "creative_line_count": 2,
                    "episode_ids": [imported["current_revision"]["episodes"][0]["id"]],
                }
            )
            run_id = "api-batch-archive-run"
            records = [
                api._catalog.save_production_record(
                    {
                        "draft_id": draft["id"],
                        "job_id": f"api-batch-job-{index}",
                        "status": status,
                        "metadata": {"production_run_id": run_id},
                    }
                )
                for index, status in ((1, "completed"), (2, "failed"))
            ]
            batch_id = str(records[0]["batch_id"])
            self.assertEqual({item["batch_id"] for item in records}, {batch_id})
            jobs = [
                RenderJob(
                    id=str(record["job_id"]),
                    batch_id=batch_id,
                    platform_id=platform.id,
                    source_file=str(root / "story.txt"),
                    title="Batch Archive Story",
                    code="BAT001",
                    video_folder=str(root),
                    music_folder=str(root),
                    output_folder=str(root),
                    status=JobStatus(str(record["status"])),
                    production_record_id=str(record["id"]),
                )
                for record in records
            ]
            queue.enqueue_jobs(jobs, platform)

            archived = api.archive_batch(batch_id)
            self.assertTrue(archived["ok"], archived)
            self.assertEqual(archived["data"]["archived_count"], 2)
            self.assertEqual(queue.list_jobs(), [])
            repeated = api.archive_batch(batch_id)
            self.assertTrue(repeated["ok"], repeated)
            self.assertTrue(repeated["data"]["already_archived"])

            restored = api.restore_batch(batch_id)
            self.assertTrue(restored["ok"], restored)
            self.assertEqual(restored["data"]["restored_count"], 2)
            self.assertEqual(
                {item["id"] for item in queue.list_jobs()},
                {"api-batch-job-1", "api-batch-job-2"},
            )
            repeated_restore = api.restore_batch(batch_id)
            self.assertTrue(repeated_restore["ok"], repeated_restore)
            self.assertTrue(repeated_restore["data"]["already_restored"])
            self.assertEqual(len(queue.list_jobs()), 2)

    def test_api_rejects_archiving_queued_or_rendering_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SettingsRepository(root / "state")
            queue = JobQueue(lambda *_: "")
            api = StoryForgeApi(repository=repository, queue=queue)
            saved_platform = api.save_platform({"name": "Active Platform"})
            self.assertTrue(saved_platform["ok"], saved_platform)
            platform = api._state.platforms[0]
            job = RenderJob(
                id="active-api-job",
                batch_id="batch",
                platform_id=platform.id,
                source_file=str(root / "story.txt"),
                title="Active Story",
                code="ACTIVE",
                video_folder=str(root),
                music_folder=str(root),
                output_folder=str(root),
                status=JobStatus.RENDERING,
            )
            queue.enqueue_jobs([job], platform)

            rejected = api.archive_job(job.id)

            self.assertFalse(rejected["ok"])
            self.assertIn("finished", rejected["error"])
            self.assertEqual(queue.list_jobs()[0]["id"], job.id)

    def test_web_allowed_roots_are_existing_local_non_root_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            allowed = root / "browser-assets"
            allowed.mkdir()
            state = ApplicationState(SettingsRepository(root / "state"))
            saved = state.update_settings(
                {"hub": {"web_allowed_roots": [str(allowed), str(allowed)]}}
            )
            self.assertEqual(saved.hub.web_allowed_roots, [str(allowed.resolve())])
            with self.assertRaisesRegex(ValueError, "路径字符串列表"):
                state.update_settings({"hub": {"web_allowed_roots": str(allowed)}})
            with self.assertRaisesRegex(ValueError, "UNC"):
                state.update_settings(
                    {"hub": {"web_allowed_roots": [r"\\server\share"]}}
                )
            with self.assertRaisesRegex(ValueError, "非根目录"):
                state.update_settings(
                    {"hub": {"web_allowed_roots": [str(Path(allowed.anchor))]}}
                )
            with self.assertRaisesRegex(ValueError, "已在 Hub 主机上存在"):
                state.update_settings(
                    {"hub": {"web_allowed_roots": [str(root / "missing")]}}
                )

    def test_visual_presets_and_custom_values_round_trip_through_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))

            saved = api.save_settings(
                {
                    "subtitle_preset": "word_pop_sync",
                    "subtitle": {
                        "active_color": "#12ABEF",
                        "pop_scale": 126,
                        "pop_intensity": 0.8,
                    },
                    "intro_card_preset": "cinematic_dark",
                    "intro_card": {
                        "position_x_percent": 56,
                        "width_percent": 70,
                        "radius": 40,
                    },
                    "code_card_preset": "light_chip",
                    "code_card": {"position_y_percent": 12},
                    "outro_card_preset": "brand_focus",
                    "outro_card": {"text_alignment": "left"},
                }
            )

            self.assertTrue(saved["ok"], saved)
            settings = saved["data"]
            self.assertEqual(settings["subtitle_preset"], "clear_outline")
            self.assertEqual(settings["subtitle_word_mode"], "single")
            self.assertFalse(settings["subtitle"]["word_sync_enabled"])
            self.assertEqual(settings["subtitle"]["active_color"], "#12ABEF")
            self.assertEqual(settings["intro_card"]["background_color"], "#111827")
            self.assertEqual(settings["intro_card"]["position_x_percent"], 56.0)
            self.assertEqual(settings["outro_card"]["text_alignment"], "left")

            bootstrap = api.get_bootstrap()["data"]
            presets = bootstrap["visual_style_presets"]
            self.assertNotIn("word_pop_sync", presets["subtitle"])
            self.assertNotIn("minimal_bottom", presets["subtitle"])
            self.assertIn("cinematic_dark", presets["intro_card"])
            self.assertEqual(
                api.get_visual_style_presets()["data"]["code_card"]["light_chip"]["radius"],
                14,
            )
            reloaded = SettingsRepository(Path(temp)).load()[0]
            self.assertEqual(reloaded.subtitle.pop_scale, 126)
            self.assertEqual(reloaded.intro_card.radius, 40)

    def test_expanded_visual_catalog_and_caption_animations_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            state = ApplicationState(repository)
            api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))

            presets = api.get_visual_style_presets()["data"]
            self.assertGreaterEqual(len(presets["subtitle"]), 11)
            self.assertGreaterEqual(len(presets["intro_card"]), 10)
            self.assertGreaterEqual(len(presets["code_card"]), 8)
            for preset in (
                "romance_glow",
                "suspense_noir",
                "confession_clean",
                "golden_hook",
                "midnight_reader",
            ):
                self.assertIn(preset, presets["subtitle"])
            self.assertNotIn("word_pop_sync", presets["subtitle"])
            self.assertNotIn("minimal_bottom", presets["subtitle"])
            for preset in (
                "social_post",
                "paper_note",
                "golden_luxe",
                "suspense_red",
                "blue_glass",
                "warm_story",
            ):
                self.assertIn(preset, presets["intro_card"])
            for preset in (
                "warning_red",
                "golden_ticket",
                "romance_blush",
                "minimal_dark",
            ):
                self.assertIn(preset, presets["code_card"])
            self.assertEqual(
                presets["subtitle"]["golden_hook"]["text_color"],
                "#FFE06A",
            )
            self.assertEqual(
                presets["intro_card"]["suspense_red"]["border_width"],
                3,
            )

            for animation in (
                "none",
                "fade",
                "soft_pop",
                "rise",
                "mask_reveal",
                "typewriter",
            ):
                with self.subTest(animation=animation):
                    saved = state.update_settings({"subtitle_animation": animation})
                    self.assertEqual(saved.subtitle_animation, animation)
            with self.assertRaisesRegex(ValueError, "字幕动画"):
                state.update_settings({"subtitle_animation": "arbitrary_ass"})

            for animation in (
                "none",
                "fade_rise",
                "soft_scale",
                "side_reveal",
                "layered_story",
                "paper_drop",
            ):
                with self.subTest(intro_animation=animation):
                    saved = state.update_settings({"intro_animation": animation})
                    self.assertEqual(saved.intro_animation, animation)
            with self.assertRaisesRegex(ValueError, "简介卡动画"):
                state.update_settings({"intro_animation": "raw_javascript"})

            for grade in (
                "neutral",
                "suspense_cool",
                "romance_warm",
                "sad_muted",
                "revenge_contrast",
                "night_lift",
            ):
                with self.subTest(color_grade=grade):
                    saved = state.update_settings({"color_grade": grade})
                    self.assertEqual(saved.color_grade, grade)
            with self.assertRaisesRegex(ValueError, "调色"):
                state.update_settings({"color_grade": "shell_filter"})

            for animation in (
                "vertical_drift",
                "focus_reveal",
                "cinematic_push",
                "ken_burns_left",
                "ken_burns_right",
                "soft_flash",
            ):
                with self.subTest(cover_animation=animation):
                    saved = state.update_settings({"cover_animation": animation})
                    self.assertEqual(saved.cover_animation, animation)
            self.assertFalse(
                state.update_settings(
                    {"cover_outro_enabled": False}
                ).cover_outro_enabled
            )
            with self.assertRaisesRegex(ValueError, "cover_outro_enabled"):
                state.update_settings({"cover_outro_enabled": "false"})

    def test_visual_style_validation_rejects_unsafe_custom_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = ApplicationState(SettingsRepository(Path(temp)))
            with self.assertRaisesRegex(ValueError, "color"):
                state.update_settings(
                    {"intro_card": {"background_color": "javascript:red"}}
                )
            with self.assertRaisesRegex(ValueError, "between"):
                state.update_settings(
                    {"outro_card": {"position_x_percent": 99}}
                )
            with self.assertRaisesRegex(ValueError, "unsupported"):
                state.update_settings({"code_card": {"raw_ass": r"{\\p1}"}})

    def test_desktop_api_exposes_no_recursive_public_object_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))
            public_data = [
                name
                for name in dir(api)
                if not name.startswith("_") and not callable(getattr(api, name))
            ]
            self.assertEqual(public_data, [])

    def test_platform_branding_round_trips_and_missing_logo_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SettingsRepository(root / "state")
            logo = root / "novelbox.png"
            logo.write_bytes(b"platform-logo")
            api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))

            saved = api.save_platform(
                {
                    "id": "novelbox",
                    "name": "NovelBox",
                    "logo_path": str(logo),
                    "brand_color": "#E94B5F",
                }
            )

            self.assertTrue(saved["ok"], saved)
            self.assertEqual(saved["data"]["logo_path"], str(logo.resolve()))
            self.assertEqual(saved["data"]["logo_uri"], logo.resolve().as_uri())
            self.assertEqual(saved["data"]["brand_color"], "#E94B5F")

            # A legacy editor omitting the new keys must not erase branding.
            edited = api.save_platform({"id": "novelbox", "name": "NovelBox"})
            self.assertEqual(edited["data"]["brand_color"], "#E94B5F")
            reloaded = SettingsRepository(root / "state").load()[1][0]
            self.assertEqual(reloaded.logo_path, str(logo.resolve()))
            self.assertEqual(reloaded.brand_color, "#E94B5F")

            logo.unlink()
            with mock.patch("storyforge.api.system_snapshot", return_value={}):
                platform = api.get_bootstrap()["data"]["platforms"][0]
            self.assertEqual(platform["logo_path"], "")
            self.assertEqual(platform["logo_uri"], "")
            self.assertEqual(platform["brand_color"], "#E94B5F")

    def test_nested_setting_updates_merge_and_invalid_retention_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            repository._protector = _PortableProtector()
            state = ApplicationState(repository)
            state.update_settings(
                {
                    "subtitle": {"font_size": 66},
                    "providers": {"text_provider": "groq"},
                }
            )
            self.assertEqual(state.settings.subtitle.font_size, 66)
            self.assertEqual(state.settings.subtitle.font_family, "Arial")
            self.assertEqual(state.settings.providers.text_provider, "groq")
            with self.assertRaisesRegex(ValueError, "保留比例"):
                state.update_settings({"retention_min": 0.95, "retention_max": 0.80})
            with self.assertRaisesRegex(ValueError, "WPM"):
                state.update_settings({"narration_wpm": 281})
            state.update_settings(
                {
                    "narration_wpm": 260,
                    "video_playback_speed": 3.0,
                    "video_transition": "fade",
                    "subtitle_word_mode": "cumulative",
                    "bgm_mode": "none",
                }
            )
            self.assertEqual(state.settings.narration_wpm, 260)
            self.assertEqual(state.settings.video_playback_speed, 3.0)
            self.assertEqual(state.settings.video_transition, "fade")
            self.assertEqual(state.settings.subtitle_word_mode, "cumulative")
            with self.assertRaisesRegex(ValueError, "背景音乐"):
                state.update_settings({"bgm_volume": 1.01})
            with self.assertRaisesRegex(ValueError, "背景音乐"):
                state.update_settings({"bgm_volume": float("nan")})
            with self.assertRaisesRegex(ValueError, "视频模板"):
                state.update_settings({"video_template": "unknown-template"})
            state.update_settings({"output_fps": 30})
            self.assertEqual(state.settings.output_fps, 30)
            with self.assertRaisesRegex(ValueError, "30 或 60 FPS"):
                state.update_settings({"output_fps": 24})

    def test_api_masks_saved_keys_and_mask_placeholder_preserves_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            repository._protector = _PortableProtector()
            with mock.patch("storyforge.api.system_snapshot", return_value={}):
                api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))
                saved = api.save_settings(
                    {
                        "providers": {
                            "text_provider": "groq",
                            "text_api_key": "text-key",
                            "tts_provider": "deepgram",
                            "tts_api_key": "tts-key",
                        }
                    }
                )
                self.assertTrue(saved["ok"])
                self.assertEqual(saved["data"]["providers"]["text_api_key"], MASKED_SECRET)
                self.assertEqual(saved["data"]["providers"]["tts_api_key"], MASKED_SECRET)

                second = api.save_settings(
                    {
                        "providers": {
                            "text_api_key": MASKED_SECRET,
                            "tts_api_key": MASKED_SECRET,
                            "allow_provider_fallback": False,
                        }
                    }
                )
                self.assertTrue(second["ok"])
                self.assertEqual(api._state.settings.providers.text_api_key, "text-key")
                self.assertEqual(api._state.settings.providers.tts_api_key, "tts-key")
                self.assertFalse(api._state.settings.providers.allow_provider_fallback)

                bootstrap = api.get_bootstrap()["data"]["settings"]["providers"]
                self.assertEqual(bootstrap["text_api_key"], MASKED_SECRET)
                self.assertEqual(bootstrap["tts_api_key"], MASKED_SECRET)
                self.assertTrue(bootstrap["has_text_api_key"])
                self.assertTrue(bootstrap["has_tts_api_key"])

    def test_platform_referenced_by_batch_cannot_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SettingsRepository(root / "state")
            repository._protector = _PortableProtector()
            state = ApplicationState(repository)
            platform = state.upsert_platform(
                {"id": "platform-1", "name": "NovelBox"}
            )
            state.batches.append(_batch(root, platform.id))
            with self.assertRaisesRegex(ValueError, "批次"):
                state.delete_platform(platform.id)

    def test_start_queue_rejects_incomplete_cloud_provider_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            repository._protector = _PortableProtector()
            api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))
            api._state.settings.providers.text_provider = "cloudflare"
            api._state.settings.providers.text_api_key = "token"
            api._state.settings.providers.tts_provider = "deepgram"
            api._state.settings.providers.tts_api_key = "voice-token"

            result = api.start_queue()

            self.assertFalse(result["ok"])
            self.assertIn("完整 API 地址", result["error"])

    def test_start_queue_explains_unconfigured_source_kokoro_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            repository._protector = _PortableProtector()
            api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))
            with mock.patch(
                "storyforge.api.embedded_kokoro_available", return_value=False
            ):
                result = api.start_queue()

            self.assertFalse(result["ok"])
            self.assertIn("Python 环境", result["error"])

    def test_start_queue_explains_unconfigured_frozen_kokoro_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            repository._protector = _PortableProtector()
            api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))
            with (
                mock.patch(
                    "storyforge.api.embedded_kokoro_available", return_value=False
                ),
                mock.patch("storyforge.api.sys.frozen", True, create=True),
            ):
                result = api.start_queue()

            self.assertFalse(result["ok"])
            self.assertIn("未安装或未检测到 Kokoro 本地组件", result["error"])

    def test_start_queue_accepts_configured_kokoro_http_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp))
            repository._protector = _PortableProtector()
            api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))
            api._state.settings.providers.kokoro_endpoint = "http://127.0.0.1:8880"
            with mock.patch(
                "storyforge.api.embedded_kokoro_available", return_value=False
            ):
                result = api.start_queue()

            self.assertTrue(result["ok"])

    def test_start_queue_uses_each_pending_jobs_frozen_provider_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SettingsRepository(root / "state")
            platform = PlatformProfile(id="platform-1", name="NovelBox")
            queue = JobQueue(lambda *_: str(root / "done.mp4"))
            queue.enqueue_jobs(
                [
                    RenderJob(
                        batch_id="batch",
                        platform_id=platform.id,
                        source_file=__file__,
                        title="Story",
                        code="B73165",
                        video_folder=str(root),
                        music_folder=str(root),
                        output_folder=str(root),
                        settings_snapshot={
                            "providers": {
                                "text_provider": "local",
                                "tts_provider": "local_kokoro",
                                "kokoro_endpoint": "http://127.0.0.1:8880",
                            }
                        },
                    )
                ],
                platform,
            )
            api = StoryForgeApi(repository=repository, queue=queue)
            api._state.settings.providers.tts_provider = "deepgram"
            api._state.settings.providers.tts_api_key = ""

            result = api.start_queue()

            self.assertTrue(result["ok"], result)
            _join_queue(self, queue)

    def test_clear_finished_jobs_returns_only_queued_and_active_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch = _batch(root)
            text_root = Path(batch.text_folder)
            for code, title in (
                ("100001", "Queued"),
                ("100002", "Rendering"),
                ("100003", "Completed"),
                ("100004", "Failed"),
                ("100005", "Cancelled"),
            ):
                (text_root / f"{code}_{title}.txt").write_text(
                    "Story.", encoding="utf-8"
                )
            queue = JobQueue(lambda *_: "")
            jobs, _ = queue.enqueue_batch(
                batch, PlatformProfile(id="platform-1", name="NovelBox")
            )
            jobs[1].status = JobStatus.RENDERING
            jobs[2].status = JobStatus.COMPLETED
            jobs[3].status = JobStatus.FAILED
            jobs[4].status = JobStatus.CANCELLED
            repository = SettingsRepository(root / "state")
            api = StoryForgeApi(repository=repository, queue=queue)

            result = api.clear_finished_jobs()

            self.assertTrue(result["ok"])
            self.assertEqual(
                [job["code"] for job in result["data"]], ["100001", "100002"]
            )
            self.assertEqual(result["data"], queue.list_jobs())

    @unittest.skipUnless(os.name == "nt", "Explorer integration is Windows-only")
    def test_open_output_folder_validates_directory_and_wraps_shell_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SettingsRepository(root / "state")
            api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))

            with mock.patch("storyforge.api.os.startfile") as startfile:
                success = api.open_output_folder(str(root))
                missing = api.open_output_folder(str(root / "missing"))
                file_path = root / "result.mp4"
                file_path.write_bytes(b"video")
                not_a_folder = api.open_output_folder(str(file_path))

            self.assertTrue(success["ok"])
            self.assertEqual(success["data"]["path"], str(root.resolve()))
            startfile.assert_called_once_with(str(root.resolve()))
            self.assertFalse(missing["ok"])
            self.assertIn("不存在或不是文件夹", missing["error"])
            self.assertFalse(not_a_folder["ok"])
            self.assertIn("不存在或不是文件夹", not_a_folder["error"])

            with mock.patch(
                "storyforge.api.os.startfile", side_effect=OSError("Explorer unavailable")
            ):
                failure = api.open_output_folder(str(root))
            self.assertFalse(failure["ok"])
            self.assertIn("Explorer unavailable", failure["error"])

    def test_open_output_folder_reports_unsupported_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = SettingsRepository(Path(temp) / "state")
            api = StoryForgeApi(repository=repository, queue=JobQueue(lambda *_: ""))
            with mock.patch("storyforge.api.os.name", "posix"):
                result = api.open_output_folder(str(temp))

            self.assertFalse(result["ok"])
            self.assertIn("仅支持 Windows", result["error"])


class JobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        # Queue unit tests exercise scheduling semantics, not the workstation's
        # live disk/memory admission policy.  Keep them deterministic when the
        # developer machine happens to be below the production safety limit.
        admission_patch = mock.patch(
            "storyforge.worker.default_heavy_job_admission",
            return_value={"allowed": True, "reason": "", "message": ""},
        )
        admission_patch.start()
        self.addCleanup(admission_patch.stop)

    @staticmethod
    def _fifo_jobs(
        prefix: str,
        count: int,
        platform: PlatformProfile,
    ) -> list[RenderJob]:
        return [
            RenderJob(
                id=f"{prefix}-{index:02d}",
                batch_id=f"batch-{prefix}",
                platform_id=platform.id,
                source_file=__file__,
                title=f"Story {prefix}",
                code=f"{prefix}{index:02d}",
                video_folder=".",
                music_folder=".",
                output_folder=".",
            )
            for index in range(count)
        ]

    def test_batch_archive_queue_operations_validate_before_mutating(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        jobs = self._fifo_jobs("ARCHIVE", 2, platform)
        for index, job in enumerate(jobs, start=1):
            job.production_record_id = f"record-{index}"
        jobs[0].status = JobStatus.COMPLETED
        jobs[1].status = JobStatus.RENDERING
        queue = JobQueue(lambda *_: "")
        queue.enqueue_jobs(jobs, platform)

        with self.assertRaisesRegex(ValueError, "every task is finished"):
            queue.archive_batch_snapshots(jobs[0].batch_id)
        self.assertEqual(len(queue.list_jobs()), 2)
        jobs[1].status = JobStatus.FAILED
        snapshots = queue.archive_batch_snapshots(jobs[0].batch_id)
        removed = queue.remove_archived_batch(
            jobs[0].batch_id, [item["id"] for item in snapshots]
        )
        self.assertEqual(set(removed), {job.id for job in jobs})
        self.assertEqual(queue.list_jobs(), [])
        restored = queue.restore_archived_batch(
            snapshots, {platform.id: platform}
        )
        self.assertEqual({item.id for item in restored}, {job.id for job in jobs})
        with self.assertRaisesRegex(ValueError, "already in the active film strip"):
            queue.restore_archived_batch(snapshots, {platform.id: platform})
        self.assertEqual(len(queue.list_jobs()), 2)

    def test_unfinished_work_includes_jobs_waiting_in_queue(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        job = self._fifo_jobs("WAITING", 1, platform)[0]
        queue = JobQueue(lambda *_: "")

        self.assertFalse(queue.has_unfinished_work())
        queue.enqueue_jobs([job], platform)
        self.assertFalse(queue.is_rendering_busy())
        self.assertTrue(queue.has_unfinished_work())
        job.status = JobStatus.COMPLETED
        self.assertFalse(queue.has_unfinished_work())

    def test_later_batch_cannot_overtake_stream_tail_appended_while_busy(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        first_batch = self._fifo_jobs("A", 13, platform)
        second_batch = self._fifo_jobs("B", 2, platform)
        cursor = 2
        first_started = threading.Event()
        release_first = threading.Event()
        processed: list[str] = []

        def loader(size: int) -> list[RenderJob]:
            nonlocal cursor
            page = first_batch[cursor : cursor + size]
            cursor += len(page)
            return page

        def processor(job, _platform, _progress):
            processed.append(job.id)
            if job.id == first_batch[0].id:
                first_started.set()
                self.assertTrue(release_first.wait(2))
            return f"{job.id}.mp4"

        queue = JobQueue(processor)
        queue.enqueue_stream(first_batch[:2], loader, platform)
        queue.start()
        self.assertTrue(first_started.wait(2))
        queue.enqueue_jobs(second_batch, platform)
        queue.start()
        release_first.set()
        _join_queue(self, queue)

        self.assertEqual(
            processed,
            [job.id for job in first_batch + second_batch],
        )

    def test_three_mixed_size_batches_remain_strict_fifo(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        first_batch = self._fifo_jobs("A", 11, platform)
        second_batch = self._fifo_jobs("B", 1, platform)
        third_batch = self._fifo_jobs("C", 10, platform)
        first_cursor = 3
        third_cursor = 1
        processed: list[str] = []

        def first_loader(size: int) -> list[RenderJob]:
            nonlocal first_cursor
            page = first_batch[first_cursor : first_cursor + size]
            first_cursor += len(page)
            return page

        def third_loader(size: int) -> list[RenderJob]:
            nonlocal third_cursor
            page = third_batch[third_cursor : third_cursor + size]
            third_cursor += len(page)
            return page

        queue = JobQueue(
            lambda job, _platform, _progress: processed.append(job.id)
            or f"{job.id}.mp4"
        )
        queue.enqueue_stream(first_batch[:3], first_loader, platform)
        queue.enqueue_jobs(second_batch, platform)
        queue.enqueue_stream(third_batch[:1], third_loader, platform)
        queue.start()
        _join_queue(self, queue)

        self.assertEqual(
            processed,
            [job.id for job in first_batch + second_batch + third_batch],
        )

    def test_failed_job_retry_is_a_new_runnable_fifo_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            platform = PlatformProfile(id="platform-1", name="NovelBox")
            failed = self._fifo_jobs("A", 1, platform)[0]
            sibling = self._fifo_jobs("B", 1, platform)[0]
            failed.output_folder = temp
            sibling.output_folder = temp
            sibling_started = threading.Event()
            release_sibling = threading.Event()
            calls: list[str] = []
            failed_once = False

            def processor(job, _platform, _progress):
                nonlocal failed_once
                calls.append(job.id)
                if job.id == failed.id and not failed_once:
                    failed_once = True
                    raise RuntimeError("retry me")
                if job.id == sibling.id:
                    sibling_started.set()
                    self.assertTrue(release_sibling.wait(2))
                return f"{job.id}.mp4"

            queue = JobQueue(processor)
            queue.enqueue_jobs([failed], platform)
            queue.enqueue_jobs([sibling], platform)
            queue.start()
            self.assertTrue(sibling_started.wait(2))

            queue.retry_failed(failed.id)
            queue.start()
            release_sibling.set()
            _join_queue(self, queue)

            self.assertEqual(calls, [failed.id, sibling.id, failed.id])
            self.assertEqual(failed.status, JobStatus.COMPLETED)

    def test_cancelled_and_cleared_head_batch_does_not_block_next_batch(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        cancelled = self._fifo_jobs("A", 2, platform)
        next_batch = self._fifo_jobs("B", 1, platform)
        processed: list[str] = []
        queue = JobQueue(
            lambda job, _platform, _progress: processed.append(job.id)
            or f"{job.id}.mp4"
        )
        queue.enqueue_jobs(cancelled, platform)
        queue.enqueue_jobs(next_batch, platform)
        queue.cancel_jobs({job.id for job in cancelled})
        queue.clear_finished()
        queue.start()
        _join_queue(self, queue)

        self.assertEqual(processed, [next_batch[0].id])

    def test_removed_archived_head_job_does_not_block_next_batch(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        archived = self._fifo_jobs("A", 1, platform)[0]
        next_batch = self._fifo_jobs("B", 1, platform)
        processed: list[str] = []
        queue = JobQueue(
            lambda job, _platform, _progress: processed.append(job.id)
            or f"{job.id}.mp4"
        )
        queue.enqueue_jobs([archived], platform)
        archived.status = JobStatus.FAILED
        queue.archive_snapshot(archived.id)
        queue.remove_archived(archived.id)
        queue.enqueue_jobs(next_batch, platform)
        queue.start()
        _join_queue(self, queue)

        self.assertEqual(processed, [next_batch[0].id])

    def test_ordinary_job_publishes_terminal_state_without_browser_poll(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        job = self._fifo_jobs("ordinary-terminal", 1, platform)[0]
        persisted: list[tuple[str, JobStatus, str]] = []
        queue = JobQueue(lambda *_args: "ordinary-output.mp4")
        queue.set_terminal_callback(
            lambda finished: persisted.append(
                (finished.id, finished.status, finished.output_file)
            )
        )
        queue.enqueue_jobs([job], platform)

        queue.start()
        _join_queue(self, queue)

        self.assertEqual(
            persisted,
            [(job.id, JobStatus.COMPLETED, "ordinary-output.mp4")],
        )

    def test_streamed_queue_processes_large_tail_with_bounded_history(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        required = {
            "batch_id": "batch",
            "platform_id": platform.id,
            "source_file": __file__,
            "title": "Story",
            "code": "B73165",
            "video_folder": ".",
            "music_folder": ".",
            "output_folder": ".",
        }
        jobs = [RenderJob(id=f"stream-{index:04d}", **required) for index in range(200)]
        cursor = 8
        processed: list[str] = []
        persisted: list[str] = []

        def loader(size: int) -> list[RenderJob]:
            nonlocal cursor
            page = jobs[cursor : cursor + size]
            cursor += len(page)
            return page

        queue = JobQueue(
            lambda job, _platform, _progress: processed.append(job.id) or f"{job.id}.mp4"
        )
        queue.set_terminal_callback(lambda job: persisted.append(job.id))
        queue.enqueue_stream(jobs[:8], loader, platform, history_limit=50)
        queue.start()
        _join_queue(self, queue)

        self.assertEqual(processed, [job.id for job in jobs])
        self.assertEqual(persisted, [job.id for job in jobs])
        self.assertLessEqual(len(queue.list_jobs()), 50)
        self.assertTrue(
            all(item["status"] == JobStatus.COMPLETED.value for item in queue.list_jobs())
        )

    def test_streamed_job_with_missing_platform_persists_terminal_failure(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        job = self._fifo_jobs("missing-platform", 1, platform)[0]
        persisted: list[tuple[str, JobStatus]] = []
        queue = JobQueue(lambda *_: "must-not-run.mp4")
        queue.set_terminal_callback(
            lambda finished: persisted.append((finished.id, finished.status))
        )
        queue.enqueue_stream([job], lambda _size: [], platform)
        queue._platforms.clear()  # simulate stale/missing runtime configuration

        queue.start()
        _join_queue(self, queue)

        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(persisted, [(job.id, JobStatus.FAILED)])

    def test_stream_loader_failure_reconnects_without_manual_restart(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        required = {
            "batch_id": "batch",
            "platform_id": platform.id,
            "source_file": __file__,
            "title": "Story",
            "code": "B73165",
            "video_folder": ".",
            "music_folder": ".",
            "output_folder": ".",
        }
        tail = RenderJob(id="tail", **required)
        attempts = 0
        processed: list[str] = []

        def loader(_size: int) -> list[RenderJob]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("temporary Hub outage")
            return [tail] if attempts == 2 else []

        queue = JobQueue(
            lambda job, _platform, _progress: processed.append(job.id) or "done.mp4"
        )
        queue.enqueue_stream([], loader, platform)
        queue.start()
        _join_queue(self, queue)
        self.assertEqual(processed, ["tail"])
        self.assertGreaterEqual(attempts, 3)
        self.assertEqual(queue.stream_status()["state"], "connected")
        self.assertTrue(queue.stream_status()["last_reconnected_at"])

    def test_cancel_discards_late_stream_page_once_without_holding_queue_lock(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        late_jobs = self._fifo_jobs("late-page", 2, platform)
        loader_entered = threading.Event()
        allow_loader_return = threading.Event()
        cancel_done = threading.Event()
        callback_done = threading.Event()
        discard_calls: list[list[str]] = []
        callback_observed_unlocked_queue: list[bool] = []

        def loader(_size: int) -> list[RenderJob]:
            loader_entered.set()
            self.assertTrue(allow_loader_return.wait(2))
            return late_jobs

        queue = JobQueue(lambda *_args: "must-not-run.mp4")

        def on_discard(jobs: list[RenderJob]) -> None:
            probe_done = threading.Event()

            def probe_queue() -> None:
                queue.list_jobs()
                probe_done.set()

            probe = threading.Thread(target=probe_queue)
            probe.start()
            callback_observed_unlocked_queue.append(probe_done.wait(0.5))
            probe.join(timeout=1)
            discard_calls.append([job.id for job in jobs])
            callback_done.set()

        queue.enqueue_stream([], loader, platform, on_discard=on_discard)
        queue.start()
        self.assertTrue(loader_entered.wait(2))

        cancel_thread = threading.Thread(
            target=lambda: (queue.cancel(), cancel_done.set())
        )
        cancel_thread.start()
        self.assertTrue(
            cancel_done.wait(0.5),
            "whole-queue cancellation waited for the blocked stream loader",
        )
        self.assertFalse(callback_done.is_set())

        allow_loader_return.set()
        cancel_thread.join(timeout=1)
        _join_queue(self, queue)

        self.assertTrue(callback_done.is_set())
        self.assertEqual(discard_calls, [[job.id for job in late_jobs]])
        self.assertEqual(callback_observed_unlocked_queue, [True])
        self.assertEqual(queue.list_jobs(), [])

    def test_selected_cancel_is_immediately_terminal_and_ignores_late_progress(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def processor(_job, _platform, progress):
            entered.set()
            self.assertTrue(release.wait(2))
            progress(JobStatus.RENDERING, 0.99, "late render callback")
            return "late-output.mp4"

        platform = PlatformProfile(id="platform-1", name="NovelBox")
        job = RenderJob(
            id="cancel-me",
            batch_id="batch",
            platform_id=platform.id,
            source_file=__file__,
            title="Story",
            code="B73165",
            video_folder=".",
            music_folder=".",
            output_folder=".",
        )
        queue = JobQueue(processor)
        queue.enqueue_jobs([job], platform)
        queue.start()
        self.assertTrue(entered.wait(2))

        changed = queue.cancel_jobs({job.id})
        self.assertEqual(changed[0]["status"], JobStatus.CANCELLED.value)
        self.assertEqual(queue.list_jobs()[0]["status"], JobStatus.CANCELLED.value)
        release.set()
        _join_queue(self, queue)
        final = queue.list_jobs()[0]
        self.assertEqual(final["status"], JobStatus.CANCELLED.value)
        self.assertEqual(final["stage_label"], "已取消")
        self.assertEqual(final["output_file"], "")

    def test_lazy_stream_tail_cancelled_during_load_never_becomes_runnable(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        tail = self._fifo_jobs("tail", 1, platform)[0]
        loader_entered = threading.Event()
        release_loader = threading.Event()
        loaded_once = False
        processed: list[str] = []
        persisted: list[tuple[str, JobStatus]] = []

        def loader(_size: int) -> list[RenderJob]:
            nonlocal loaded_once
            if loaded_once:
                return []
            loaded_once = True
            loader_entered.set()
            self.assertTrue(release_loader.wait(2))
            return [tail]

        queue = JobQueue(
            lambda job, _platform, _progress: processed.append(job.id)
            or f"{job.id}.mp4"
        )
        queue.set_terminal_callback(
            lambda job: persisted.append((job.id, job.status))
        )
        queue.enqueue_stream([], loader, platform)
        queue.start()
        self.assertTrue(loader_entered.wait(2))

        # The durable task is known to the caller but has not reached the
        # bounded in-memory window yet.
        self.assertEqual(queue.cancel_jobs({tail.id}), [])
        release_loader.set()
        _join_queue(self, queue)

        self.assertEqual(processed, [])
        self.assertEqual(tail.status, JobStatus.CANCELLED)
        self.assertEqual(persisted, [(tail.id, JobStatus.CANCELLED)])

    def test_unmatched_lazy_cancel_tombstone_is_cleared_after_loaders_exhaust(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        loader_entered = threading.Event()
        release_loader = threading.Event()

        def loader(_size: int) -> list[RenderJob]:
            loader_entered.set()
            self.assertTrue(release_loader.wait(2))
            # The durable catalog already filtered out the cancelled record.
            return []

        queue = JobQueue(lambda *_args: "must-not-run.mp4")
        queue.enqueue_stream([], loader, platform)
        queue.start()
        self.assertTrue(loader_entered.wait(2))
        queue.cancel_jobs({"catalog-filtered-tail"})
        self.assertIn("catalog-filtered-tail", queue._lazy_cancelled_job_ids)

        release_loader.set()
        _join_queue(self, queue)

        self.assertNotIn("catalog-filtered-tail", queue._lazy_cancelled_job_ids)
        self.assertNotIn("catalog-filtered-tail", queue._cancelled_job_ids)

    def test_cancelled_running_job_cannot_be_retried_archived_or_cleared_early(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        job = self._fifo_jobs("active", 1, platform)[0]
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def processor(item, _platform, _progress):
            calls.append(item.id)
            entered.set()
            self.assertTrue(release.wait(2))
            return f"attempt-{len(calls)}.mp4"

        queue = JobQueue(processor)
        queue.enqueue_jobs([job], platform)
        queue.start()
        self.assertTrue(entered.wait(2))
        queue.cancel_jobs({job.id})

        with self.assertRaisesRegex(ValueError, "仍在停止"):
            queue.retry_failed(job.id)
        with self.assertRaisesRegex(ValueError, "仍在停止"):
            queue.archive_snapshot(job.id)
        self.assertEqual(
            [item["id"] for item in queue.clear_finished()],
            [job.id],
        )

        release.set()
        _join_queue(self, queue)
        self.assertEqual(job.status, JobStatus.CANCELLED)
        self.assertEqual(job.output_file, "")

        queue.retry_failed(job.id)
        queue.start()
        _join_queue(self, queue)
        self.assertEqual(calls, [job.id, job.id])
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.output_file, "attempt-2.mp4")

    def test_failed_terminal_callback_auto_reconnects_without_overtaking(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        first, tail = self._fifo_jobs("A", 2, platform)
        younger = self._fifo_jobs("B", 1, platform)[0]
        cursor = 0
        callback_available = False
        processed: list[str] = []
        persisted: list[str] = []

        def first_loader(_size: int) -> list[RenderJob]:
            nonlocal cursor
            if cursor:
                return []
            cursor = 1
            return [tail]

        def terminal_callback(job: RenderJob) -> None:
            if not callback_available:
                raise OSError("ledger unavailable")
            persisted.append(job.id)

        queue = JobQueue(
            lambda job, _platform, _progress: processed.append(job.id)
            or f"{job.id}.mp4"
        )
        queue.set_terminal_callback(terminal_callback)
        queue.enqueue_stream([first], first_loader, platform, history_limit=1)
        queue.start()
        deadline = threading.Event()
        for _attempt in range(200):
            if queue.stream_status()["reconnecting"]:
                break
            deadline.wait(0.01)
        self.assertTrue(queue.stream_status()["reconnecting"])
        self.assertEqual(processed, [first.id])

        # Queuing another batch must not overtake the unsaved terminal result.
        queue.enqueue_stream([younger], lambda _size: [], platform, history_limit=1)
        queue.start()
        self.assertEqual(processed, [first.id])
        self.assertEqual([item["id"] for item in queue.list_jobs()], [first.id, younger.id])

        callback_available = True
        _join_queue(self, queue)
        self.assertEqual(processed, [first.id, tail.id, younger.id])
        self.assertEqual(persisted, [first.id, tail.id, younger.id])
        self.assertEqual(queue.stream_status()["state"], "connected")

    def test_wrong_platform_stream_page_becomes_terminal_without_worker_crash(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        wrong_platform = PlatformProfile(id="platform-2", name="Other")
        invalid = self._fifo_jobs("wrong", 1, wrong_platform)[0]
        loaded_once = False
        processed: list[str] = []
        persisted: list[tuple[str, JobStatus]] = []
        thread_errors: list[str] = []
        original_hook = threading.excepthook

        def loader(_size: int) -> list[RenderJob]:
            nonlocal loaded_once
            if loaded_once:
                return []
            loaded_once = True
            return [invalid]

        threading.excepthook = lambda args: thread_errors.append(str(args.exc_value))
        try:
            queue = JobQueue(
                lambda job, _platform, _progress: processed.append(job.id)
                or f"{job.id}.mp4"
            )
            queue.set_terminal_callback(
                lambda job: persisted.append((job.id, job.status))
            )
            queue.enqueue_stream([], loader, platform)
            queue.start()
            _join_queue(self, queue)
        finally:
            threading.excepthook = original_hook

        self.assertEqual(thread_errors, [])
        self.assertEqual(processed, [])
        self.assertEqual(invalid.status, JobStatus.FAILED)
        self.assertEqual(persisted, [(invalid.id, JobStatus.FAILED)])

    def test_job_added_while_worker_is_busy_is_processed_without_second_start(self) -> None:
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def processor(job, _platform, _progress):
            calls.append(job.id)
            if job.id == "first":
                started.set()
                self.assertTrue(release.wait(2))
            return f"{job.id}.mp4"

        platform = PlatformProfile(id="platform-1", name="NovelBox")
        required = {
            "batch_id": "batch",
            "platform_id": platform.id,
            "source_file": __file__,
            "title": "Story",
            "code": "B73165",
            "video_folder": ".",
            "music_folder": ".",
            "output_folder": ".",
        }
        queue = JobQueue(processor)
        queue.enqueue_jobs([RenderJob(id="first", **required)], platform)
        queue.start()
        self.assertTrue(started.wait(2))
        queue.enqueue_jobs([RenderJob(id="second", **required)], platform)
        queue.start()  # intentionally a no-op while the first worker is alive
        release.set()
        _join_queue(self, queue)

        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(
            [item["status"] for item in queue.list_jobs()],
            ["completed", "completed"],
        )

    def test_job_enqueued_as_worker_retires_is_processed_by_replacement(self) -> None:
        retirement_committed = threading.Event()
        release_retiring_worker = threading.Event()
        calls: list[str] = []

        def processor(job, _platform, _progress):
            calls.append(job.id)
            return f"{job.id}.mp4"

        platform = PlatformProfile(id="platform-1", name="NovelBox")
        required = {
            "batch_id": "batch",
            "platform_id": platform.id,
            "source_file": __file__,
            "title": "Story",
            "code": "B73165",
            "video_folder": ".",
            "music_folder": ".",
            "output_folder": ".",
        }
        queue = JobQueue(processor)
        original_retire_if_idle = queue._retire_if_idle
        held_once = False

        def controlled_retirement(observed_revision: int) -> bool:
            nonlocal held_once
            retired = original_retire_if_idle(observed_revision)
            if retired and not held_once:
                held_once = True
                retirement_committed.set()
                self.assertTrue(release_retiring_worker.wait(2))
            return retired

        queue._retire_if_idle = controlled_retirement  # type: ignore[method-assign]
        queue.enqueue_jobs([RenderJob(id="first", **required)], platform)
        queue.start()
        first_worker = queue._worker
        self.assertIsNotNone(first_worker)
        self.assertTrue(retirement_committed.wait(2))

        queue.enqueue_jobs([RenderJob(id="second", **required)], platform)
        queue.start()
        replacement_worker = queue._worker
        self.assertIsNotNone(replacement_worker)
        self.assertIsNot(replacement_worker, first_worker)

        release_retiring_worker.set()
        assert first_worker is not None
        assert replacement_worker is not None
        first_worker.join(timeout=2)
        replacement_worker.join(timeout=2)
        self.assertFalse(first_worker.is_alive())
        self.assertFalse(replacement_worker.is_alive())
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(
            [item["status"] for item in queue.list_jobs()],
            ["completed", "completed"],
        )

    def test_scan_enqueue_and_process_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch = _batch(root)
            text_root = Path(batch.text_folder)
            (text_root / "B73165_A Quiet Door.txt").write_text(
                "A complete sentence.", encoding="utf-8"
            )
            (text_root / "bad-name.txt").write_text("ignored", encoding="utf-8")
            (text_root / "999_Metadata.meta.txt").write_text("ignored", encoding="utf-8")
            calls: list[str] = []

            def processor(job, platform, progress):
                calls.append(job.code)
                self.assertEqual(platform.name, "NovelBox")
                progress(JobStatus.NARRATING, 0.4, "voice")
                output = Path(job.output_folder) / f"{job.code}.mp4"
                output.write_bytes(b"video")
                return str(output)

            queue = JobQueue(processor)
            platform = PlatformProfile(id="platform-1", name="NovelBox")
            jobs, errors = queue.enqueue_batch(batch, platform)
            self.assertEqual([job.code for job in jobs], ["B73165"])
            self.assertEqual(
                jobs[0].settings_snapshot["output_mode"],
                "video_and_mp3",
            )
            self.assertFalse(
                jobs[0].settings_snapshot["export_narration_audio"]
            )
            self.assertEqual(len(errors), 1)
            self.assertIn("bad-name.txt", errors[0])
            queue.start()
            _join_queue(self, queue)

            result = queue.list_jobs()[0]
            self.assertEqual(calls, ["B73165"])
            self.assertEqual(result["status"], JobStatus.COMPLETED.value)
            self.assertEqual(result["progress"], 1.0)
            self.assertTrue(Path(result["output_file"]).is_file())

    def test_legacy_audio_only_batch_freezes_audio_contract_on_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch = _batch(root)
            batch.output_mode = "audio_only"
            batch.video_folder = ""
            batch.music_folder = ""
            Path(batch.text_folder, "AUDIO01_Story.txt").write_text(
                "Story.", encoding="utf-8"
            )
            queue = JobQueue(lambda *_: "")
            jobs, errors = queue.enqueue_batch(
                batch,
                PlatformProfile(id="platform-1", name="NovelBox"),
            )

            self.assertEqual(errors, [])
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].settings_snapshot["output_mode"], "audio_only")
            self.assertFalse(jobs[0].settings_snapshot["export_narration_audio"])

    def test_processor_failure_does_not_stop_remaining_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch = _batch(root)
            text_root = Path(batch.text_folder)
            (text_root / "111111_First.txt").write_text("First.", encoding="utf-8")
            (text_root / "222222_Second.txt").write_text("Second.", encoding="utf-8")

            def processor(job, _platform, _progress):
                if job.code == "111111":
                    raise RuntimeError("synthetic provider failure")
                return str(Path(job.output_folder) / "second.mp4")

            queue = JobQueue(processor)
            queue.enqueue_batch(
                batch, PlatformProfile(id="platform-1", name="NovelBox")
            )
            queue.start()
            _join_queue(self, queue)

            first, second = queue.list_jobs()
            self.assertEqual(first["status"], JobStatus.FAILED.value)
            self.assertIn("synthetic provider failure", first["message"])
            self.assertTrue(first["error_log"])
            error_log = Path(first["error_log"])
            self.assertTrue(error_log.is_file())
            self.assertIn("synthetic provider failure", error_log.read_text(encoding="utf-8"))
            self.assertEqual(second["status"], JobStatus.COMPLETED.value)

    def test_processor_preserves_primary_render_log_and_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch = _batch(root)
            Path(batch.text_folder, "111111_First.txt").write_text(
                "First.", encoding="utf-8"
            )
            render_log = root / "render-error.log"
            render_log.write_text("No space left on device", encoding="utf-8")
            diagnostics = {
                "schema_version": 1,
                "code": "disk_full",
                "summary": "制作电脑的可用磁盘空间不足。",
            }

            class RenderFailure(RuntimeError):
                error_log = str(render_log)
                failure_diagnostics = diagnostics

            def processor(*_args):
                raise RenderFailure("render failed")

            queue = JobQueue(processor)
            queue.enqueue_batch(
                batch, PlatformProfile(id="platform-1", name="NovelBox")
            )
            queue.start()
            _join_queue(self, queue)

            failed = queue.list_jobs()[0]
            self.assertEqual(failed["error_log"], str(render_log))
            self.assertEqual(failed["failure_diagnostics"], diagnostics)

    def test_empty_supplied_diagnostics_falls_back_to_persisted_failure_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch = _batch(root)
            Path(batch.text_folder, "111111_First.txt").write_text(
                "First.", encoding="utf-8"
            )

            class PreflightFailure(RuntimeError):
                error_log = ""
                failure_diagnostics: dict[str, object] = {}

            def processor(*_args):
                raise PreflightFailure("Permission denied while opening output")

            queue = JobQueue(processor)
            queue.enqueue_batch(
                batch, PlatformProfile(id="platform-1", name="NovelBox")
            )
            queue.start()
            _join_queue(self, queue)

            failed = queue.list_jobs()[0]
            self.assertTrue(Path(failed["error_log"]).is_file())
            self.assertEqual(
                failed["failure_diagnostics"]["code"], "permission_denied"
            )
            self.assertIn(
                "Permission denied", failed["failure_diagnostics"]["log_tail"]
            )

    def test_processor_can_be_attached_but_not_replaced_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch = _batch(root)
            Path(batch.text_folder, "123456_Story.txt").write_text(
                "Story.", encoding="utf-8"
            )
            entered = threading.Event()
            release = threading.Event()

            def processor(*_args):
                entered.set()
                self.assertTrue(release.wait(timeout=3))
                return "done.mp4"

            queue = JobQueue()
            with self.assertRaisesRegex(RuntimeError, "尚未初始化"):
                queue.start()
            queue.set_processor(processor)
            queue.enqueue_batch(
                batch, PlatformProfile(id="platform-1", name="NovelBox")
            )
            queue.start()
            self.assertTrue(entered.wait(timeout=2))
            with self.assertRaisesRegex(RuntimeError, "运行时"):
                queue.set_processor(lambda *_: "replacement.mp4")
            release.set()
            _join_queue(self, queue)

    def test_preview_waits_for_approval_without_blocking_then_renders_full(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch = _batch(root)
            platform = PlatformProfile(id="platform-1", name="NovelBox")
            first = RenderJob(
                batch_id=batch.id,
                platform_id=platform.id,
                source_file=str(Path(batch.text_folder) / "A1_First.txt"),
                title="First",
                code="A1",
                video_folder=batch.video_folder,
                music_folder=batch.music_folder,
                output_folder=batch.output_folder,
                job_kind="preview",
            )
            second = RenderJob(
                batch_id=batch.id,
                platform_id=platform.id,
                source_file=str(Path(batch.text_folder) / "B2_Second.txt"),
                title="Second",
                code="B2",
                video_folder=batch.video_folder,
                music_folder=batch.music_folder,
                output_folder=batch.output_folder,
            )
            calls: list[tuple[str, str]] = []

            def processor(job, _platform, _progress):
                calls.append((job.code, job.job_kind))
                suffix = "preview" if job.job_kind == "preview" else "full"
                output = Path(job.output_folder) / f"{job.code}-{suffix}.mp4"
                output.write_bytes(b"video")
                return str(output)

            queue = JobQueue(processor)
            queue.enqueue_jobs([first, second], platform)
            queue.start()
            _join_queue(self, queue)

            first_state, second_state = queue.list_jobs()
            self.assertEqual(first_state["status"], JobStatus.AWAITING_APPROVAL.value)
            self.assertTrue(first_state["preview_file"].endswith("A1-preview.mp4"))
            self.assertEqual(second_state["status"], JobStatus.COMPLETED.value)
            self.assertEqual(calls, [("A1", "preview"), ("B2", "full")])

            queue.approve_preview(first.id)
            queue.start()
            _join_queue(self, queue)
            approved = queue.list_jobs()[0]
            self.assertEqual(approved["status"], JobStatus.COMPLETED.value)
            self.assertTrue(approved["preview_approved"])
            self.assertTrue(approved["output_file"].endswith("A1-full.mp4"))

    def test_changed_settings_invalidate_awaiting_sample_and_hold_related_jobs(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        required = {
            "batch_id": "batch",
            "platform_id": platform.id,
            "source_file": __file__,
            "title": "Series",
            "code": "B73165",
            "video_folder": ".",
            "music_folder": ".",
            "output_folder": ".",
            "production_draft_id": "draft-1",
            "settings_snapshot": {"narration_wpm": 190},
        }
        sample = RenderJob(id="sample", job_kind="preview", **required)
        sample.status = JobStatus.AWAITING_APPROVAL
        sample.preview_file = "old-sample.mp4"
        full = RenderJob(id="full", job_kind="full", **required)
        full.status = JobStatus.WAITING_PREVIEW
        queue = JobQueue(lambda *_: "")
        queue.enqueue_jobs([sample, full], platform)

        invalidated = queue.invalidate_awaiting_previews(
            {"narration_wpm": 210}
        )

        self.assertEqual(invalidated, ["sample"])
        self.assertEqual(sample.status, JobStatus.QUEUED)
        self.assertEqual(sample.preview_file, "")
        self.assertEqual(sample.settings_snapshot["narration_wpm"], 210)
        self.assertEqual(full.status, JobStatus.WAITING_PREVIEW)
        self.assertEqual(full.settings_snapshot["narration_wpm"], 210)

    def test_clear_finished_preserves_every_nonterminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            batch = _batch(root)
            text_root = Path(batch.text_folder)
            statuses = (
                JobStatus.QUEUED,
                JobStatus.PREFLIGHT,
                JobStatus.PREPARING,
                JobStatus.POLISHING,
                JobStatus.NARRATING,
                JobStatus.COMPOSING,
                JobStatus.PREVIEWING,
                JobStatus.AWAITING_APPROVAL,
                JobStatus.APPROVED,
                JobStatus.RENDERING,
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.INTERRUPTED,
            )
            for index in range(len(statuses)):
                (text_root / f"2{index:05d}_Story {index}.txt").write_text(
                    "Story.", encoding="utf-8"
                )
            queue = JobQueue(lambda *_: "")
            jobs, _ = queue.enqueue_batch(
                batch, PlatformProfile(id="platform-1", name="NovelBox")
            )
            for job, status in zip(jobs, statuses, strict=True):
                job.status = status

            remaining = queue.clear_finished()

            self.assertEqual(
                [job["status"] for job in remaining],
                [
                    status.value
                    for status in statuses
                    if status
                    not in {
                        JobStatus.COMPLETED,
                        JobStatus.FAILED,
                        JobStatus.CANCELLED,
                        JobStatus.INTERRUPTED,
                    }
                ],
            )
            self.assertEqual(remaining, queue.list_jobs())


if __name__ == "__main__":
    unittest.main()
