from __future__ import annotations

import tempfile
import unittest
from itertools import islice
from pathlib import Path
from unittest.mock import patch

from storyforge.catalog import CatalogConflictError, CatalogRepository
from storyforge.library_service import LibraryService, _fit_intro_card_text
from storyforge.models import AppSettings, JobStatus, PlatformProfile
from storyforge.providers.base import ProviderConfigurationError
from storyforge.providers.text import TextResult
from storyforge.services.subtitles import _fit_card_lines
from storyforge.services.text_processing import analyze_manuscript


class LibraryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = AppSettings()
        self.catalog = CatalogRepository(self.root / "catalog.sqlite3")
        self.service = LibraryService(
            self.catalog,
            lambda: self.settings,
            self.root / "data",
        )
        self.platform = PlatformProfile(id="goodnovel", name="GoodNovel")
        self.service.sync_platforms([self.platform])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _import(self) -> dict:
        result = self.service.import_text(
            {
                "title": "The Midnight Call",
                "synopsis": "A stranger knows her husband's secret.",
                "text": (
                    "Chapter 1\nThe phone rang at ten. A stranger whispered my husband's name.\n\n"
                    "Chapter 2\nI drove to the address she gave me. The door was already open."
                ),
            }
        )
        return result["novel"]

    def test_novel_projection_survives_older_hub_without_voice_history_rpc(self) -> None:
        novel = self._import()

        with patch.object(
            self.catalog,
            "last_successful_voice",
            side_effect=RuntimeError(
                "Hub returned HTTP 404 (method_not_allowed): RPC method is not exposed"
            ),
        ):
            projected = self.service.novel_for_ui(novel["id"])

        self.assertEqual(projected["id"], novel["id"])
        self.assertEqual(projected["default_voice"], "待试听")

    def test_record_projection_exposes_generic_video_fallback_marker(self) -> None:
        selection = {
            "mode": "generic_fallback",
            "fallback": True,
            "requested_category": "romance",
            "matched_category": None,
            "source_scope": "selected_root_recursive",
        }
        record = self.service._record_for_ui(
            {
                "id": "record-1",
                "status": "completed",
                "error_message": "通用素材回退：romance 分类无可用视频。",
                "metadata": {
                    "failure_diagnostics": {
                        "code": "corrupt_media",
                        "summary": "素材文件损坏。",
                        "log_tail": "Invalid data found when processing input",
                    },
                    "media_selection": selection,
                    "materials": [
                        {
                            "name": "generic.mp4",
                            "type": "video",
                            "usage_count": 3,
                            "selection_mode": "generic_fallback",
                            "generic_fallback": True,
                        }
                    ],
                },
            },
            {},
        )

        self.assertEqual(record["media_selection"], selection)
        self.assertTrue(record["materials"][0]["generic_fallback"])
        self.assertIn("通用素材回退", record["error"])
        self.assertEqual(record["failure_diagnostics"]["code"], "corrupt_media")

    def test_batch_recipe_freezes_complete_custom_visual_styles(self) -> None:
        recipe = self.service._validated_production_settings(
            {
                "subtitle_preset": "word_pop_sync",
                "subtitle": {
                    "active_color": "#FFCC00",
                    "pop_scale": 128,
                    "word_sync_enabled": True,
                },
                "intro_card_preset": "romance_soft",
                "intro_card": {"position_x_percent": 55},
                "code_card_preset": "outline_only",
                "code_card": {"radius": 28},
                "outro_card_preset": "cinematic_dark",
                "outro_card": {"position_y_percent": 18},
                "cover_outro_enabled": False,
            }
        )

        self.assertEqual(recipe["subtitle"]["active_color"], "#FFCC00")
        self.assertEqual(recipe["subtitle"]["pop_scale"], 128)
        self.assertEqual(recipe["intro_card"]["background_color"], "#FFF1F5")
        self.assertEqual(recipe["intro_card"]["position_x_percent"], 55.0)
        self.assertEqual(recipe["code_card"]["radius"], 28)
        self.assertEqual(recipe["outro_card"]["background_color"], "#0F172A")
        self.assertFalse(recipe["cover_outro_enabled"])
        with self.assertRaisesRegex(ValueError, "cover_outro_enabled"):
            self.service._validated_production_settings(
                {"cover_outro_enabled": "false"}
            )
        snapshot = dict(recipe)
        self.settings.intro_card.background_color = "#000000"
        self.assertEqual(snapshot["intro_card"]["background_color"], "#FFF1F5")

    def test_batch_recipe_persists_concise_preview_duration(self) -> None:
        recipe = self.service._validated_production_settings(
            {"preview_seconds": 15},
            base={"preview_seconds": 30},
        )

        self.assertEqual(recipe["preview_seconds"], 15)

    def test_draft_and_jobs_freeze_team_preset_provenance(self) -> None:
        preset = self.catalog.save_production_preset(
            {
                "name": "团队悬疑方案",
                "recipe": {
                    "story_mood": "suspense",
                    "production_settings": {
                        "narration_wpm": 222,
                        "output_fps": 60,
                        "export_narration_audio": True,
                        "cover_outro_enabled": False,
                    },
                },
            },
        )
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "PRESET01",
            }
        )["promo_code"]
        saved = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [novel["episodes"][0]["id"]],
                "target_video_count": 1,
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_bella",
                },
                "production_settings": {
                    "narration_wpm": 222,
                    "output_fps": 60,
                    "export_narration_audio": True,
                    "cover_outro_enabled": False,
                },
                "applied_production_preset_id": preset["id"],
                "applied_production_preset_revision": preset["revision"],
                "applied_production_preset_hash": preset["content_hash"],
                "production_preset_dirty": False,
            }
        )["draft"]

        self.assertEqual(saved["applied_production_preset_id"], preset["id"])
        self.assertEqual(
            saved["applied_production_preset_revision"], preset["revision"]
        )

        # An administrator editing the team preset later must not rewrite this
        # already-frozen batch.
        self.catalog.save_production_preset(
            {
                **preset,
                "recipe": {
                    "story_mood": "suspense",
                    "production_settings": {
                        "narration_wpm": 240,
                        "output_fps": 60,
                        "export_narration_audio": False,
                    },
                },
            },
        )
        video = self.root / "preset-video"
        music = self.root / "preset-music"
        output = self.root / "preset-output"
        video.mkdir()
        music.mkdir()
        _, _, jobs = self.service.build_render_jobs(
            {
                "draft_id": saved["id"],
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(output),
            }
        )
        self.assertEqual(jobs[0].production_preset_id, preset["id"])
        self.assertEqual(jobs[0].production_preset_revision, preset["revision"])
        self.assertEqual(jobs[0].production_preset_hash, preset["content_hash"])
        self.assertEqual(jobs[0].settings_snapshot["narration_wpm"], 222)
        self.assertTrue(jobs[0].settings_snapshot["export_narration_audio"])
        self.assertFalse(jobs[0].settings_snapshot["cover_outro_enabled"])
        self.assertFalse(jobs[0].cover_outro_enabled)

    def test_deleted_preset_detaches_draft_without_losing_frozen_settings(self) -> None:
        preset = self.catalog.save_production_preset(
            {
                "name": "Temporary personal recipe",
                "recipe": {
                    "story_mood": "suspense",
                    "production_settings": {
                        "narration_wpm": 260,
                        "output_fps": 60,
                        "subtitle_preset": "word_pop_sync",
                        "export_narration_audio": True,
                    },
                },
            }
        )
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "DETACH01",
            }
        )["promo_code"]
        saved = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [novel["episodes"][0]["id"]],
                "target_video_count": 1,
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_bella",
                },
                "production_settings": {
                    "narration_wpm": 260,
                    "output_fps": 60,
                    "subtitle_preset": "word_pop_sync",
                    "export_narration_audio": True,
                },
                "applied_production_preset_id": preset["id"],
                "applied_production_preset_revision": preset["revision"],
                "applied_production_preset_hash": preset["content_hash"],
                "production_preset_dirty": False,
            }
        )["draft"]

        deleted = self.catalog.delete_production_preset(preset["id"])
        self.assertTrue(deleted["deleted"])

        detached_result = self.service.save_draft(
            {
                "id": saved["id"],
                "row_version": saved["row_version"],
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [novel["episodes"][0]["id"]],
                "target_video_count": 1,
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_bella",
                },
                # This is the complete per-batch snapshot already stored on
                # the draft; the deleted preset is provenance, not a runtime
                # dependency.
                "production_settings": saved["production_settings"],
                "applied_production_preset_id": preset["id"],
                "applied_production_preset_revision": preset["revision"],
                "applied_production_preset_hash": preset["content_hash"],
                "production_preset_dirty": False,
            }
        )
        detached = detached_result["draft"]

        self.assertEqual(detached["applied_production_preset_id"], "")
        self.assertEqual(detached["applied_production_preset_revision"], 0)
        self.assertEqual(detached["applied_production_preset_hash"], "")
        self.assertTrue(detached["production_preset_dirty"])
        self.assertEqual(detached["production_settings"]["narration_wpm"], 260)
        self.assertEqual(
            detached["production_settings"]["subtitle_preset"],
            "word_pop_sync",
        )
        self.assertTrue(detached["production_settings"]["export_narration_audio"])
        self.assertTrue(detached_result["warnings"])
        self.assertIn("原制作方案", detached_result["warnings"][0])
        self.assertIn("本批自定义", detached_result["warnings"][0])

        video = self.root / "detached-video"
        music = self.root / "detached-music"
        output = self.root / "detached-output"
        video.mkdir()
        music.mkdir()
        _, _, jobs = self.service.build_render_jobs(
            {
                "draft_id": detached["id"],
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(output),
            }
        )
        self.assertEqual(jobs[0].production_preset_id, "")
        self.assertEqual(jobs[0].production_preset_revision, 0)
        self.assertEqual(jobs[0].production_preset_hash, "")
        self.assertEqual(jobs[0].settings_snapshot["narration_wpm"], 260)
        self.assertEqual(
            jobs[0].settings_snapshot["subtitle_preset"],
            "word_pop_sync",
        )

    def test_import_returns_the_ui_contract_and_deduplicates_body(self) -> None:
        novel = self._import()
        self.assertEqual(novel["title"], "The Midnight Call")
        self.assertEqual(novel["source_chapters"], 2)
        self.assertGreaterEqual(len(novel["episodes"]), 1)
        self.assertEqual(novel["episodes"][0]["number"], 1)
        duplicate = self.service.import_text(
            {
                "title": "A Different Filename",
                "text": (
                    "Chapter 1\nThe phone rang at ten. A stranger whispered my husband's name.\n\n"
                    "Chapter 2\nI drove to the address she gave me. The door was already open."
                ),
            }
        )
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(duplicate["novel"]["id"], novel["id"])

    def test_queued_draft_is_not_reopened_as_the_next_editable_batch(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "BATCH01",
            }
        )["promo_code"]
        payload = {
            "novel_id": novel["id"],
            "platform_id": self.platform.id,
            "promo_code_id": promo["id"],
            "episode_ids": [novel["episodes"][0]["id"]],
            "target_video_count": 1,
        }
        queued_draft = self.service.save_draft(payload)["draft"]
        self.catalog.save_production_record(
            {
                "draft_id": queued_draft["id"],
                "job_id": "queued-batch-job",
                "status": "queued",
            }
        )

        reopened = self.service.novel_for_ui(novel["id"])
        self.assertEqual(reopened["draft"]["id"], "")

        next_draft = self.service.save_draft(payload)["draft"]
        self.assertNotEqual(next_draft["id"], queued_draft["id"])
        reopened = self.service.novel_for_ui(novel["id"])
        self.assertEqual(reopened["draft"]["id"], next_draft["id"])

    def test_exact_duplicate_draft_returns_warning_without_blocking(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "DUP001",
            }
        )["promo_code"]
        payload = {
            "novel_id": novel["id"],
            "platform_id": self.platform.id,
            "promo_code_id": promo["id"],
            "episode_ids": [novel["episodes"][0]["id"]],
            "target_video_count": 2,
            "voice": {
                "provider": "local_kokoro",
                "voice_id": "af_heart",
            },
            "production_settings": {"narration_wpm": 240},
        }

        first = self.service.save_draft(payload)
        duplicate = self.service.save_draft(payload)

        self.assertNotEqual(first["draft"]["id"], duplicate["draft"]["id"])
        self.assertTrue(duplicate["warnings"])
        self.assertIn("完全相同", duplicate["warning"])
        self.assertEqual(duplicate["draft"]["warnings"], duplicate["warnings"])

    def test_novel_statistics_count_only_successful_video_and_hide_code_usage(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "STAT01",
            }
        )["promo_code"]
        payload = {
            "novel_id": novel["id"],
            "platform_id": self.platform.id,
            "promo_code_id": promo["id"],
            "episode_ids": [novel["episodes"][0]["id"]],
            "target_video_count": 1,
        }
        video_draft = self.service.save_draft(payload)["draft"]
        self.catalog.save_production_record(
            {
                "draft_id": video_draft["id"],
                "job_id": "successful-video",
                "status": "completed",
                "metadata": {
                    "job_snapshot": {
                        "settings_snapshot": {"output_mode": "video_and_mp3"}
                    }
                },
            }
        )
        audio_draft = self.service.save_draft(
            {
                **payload,
                "production_settings": {"output_mode": "audio_only"},
            }
        )["draft"]
        self.catalog.save_production_record(
            {
                "draft_id": audio_draft["id"],
                "job_id": "successful-audio",
                "status": "completed",
                "metadata": {
                    "job_snapshot": {
                        "settings_snapshot": {"output_mode": "audio_only"}
                    }
                },
            }
        )

        projected = self.service.novel_for_ui(novel["id"])

        self.assertEqual(projected["statistics"]["successful_video_count"], 1)
        self.assertTrue(projected["statistics"]["last_production_at"])
        self.assertNotIn("progress", projected)
        self.assertNotIn("use_count", projected["platform_bindings"][0]["codes"][0])

    def test_classification_falls_back_when_selected_provider_construction_fails(self) -> None:
        novel = self._import()
        self.settings.providers.text_provider = "groq"
        self.settings.providers.allow_provider_fallback = True
        factory_calls: list[str] = []
        test_case = self

        class LocalProvider:
            def polish(self, request) -> TextResult:
                self_request_text = request.text
                test_case.assertTrue(self_request_text)
                return TextResult(
                    polished_text=self_request_text,
                    hook="",
                    ending_cta="",
                    mood="romance",
                    provider="local",
                    model="rules",
                )

        def provider_factory(config):
            name = str(
                getattr(config, "name", "")
                or getattr(config, "text_provider", "")
            )
            factory_calls.append(name)
            if name == "groq":
                raise ProviderConfigurationError(
                    "missing cloud API key",
                    provider="groq",
                )
            return LocalProvider()

        service = LibraryService(
            self.catalog,
            lambda: self.settings,
            self.root / "fallback-data",
            text_provider_factory=provider_factory,
        )

        result = service.classify_novel(novel["id"], force=True)

        self.assertEqual(factory_calls, ["groq", "local"])
        self.assertEqual(result["classification"]["mood"], "romance")
        self.assertEqual(result["classification"]["source"], "local_fallback")
        self.assertEqual(result["classification"]["provider"], "local")
        self.assertIn("missing cloud API key", result["classification"]["warning"])

    def test_ui_episode_exposes_authored_title_and_long_episode_split(self) -> None:
        imported = self.service.import_text(
            {
                "title": "The Letter",
                "language": "en",
                "text": (
                    "Chapter 1 - The Letter Under the Door\n"
                    "Mara found a sealed letter beneath her apartment door.\n\n"
                    "Chapter 2 - The Name Inside\n"
                    "The name inside belonged to the sister she had never met."
                ),
            }
        )["novel"]

        first = imported["episodes"][0]
        self.assertEqual(first["title"], "Chapter 1 - The Letter Under the Door")
        self.assertEqual(
            first["original_title"], "Chapter 1 - The Letter Under the Door"
        )
        self.assertEqual(
            first["source_label"], "Chapter 1 - The Letter Under the Door"
        )
        self.assertTrue(first["explicit_source_boundary"])
        self.assertFalse(first["is_source_split"])

        split = self.catalog.import_novel(
            {
                "title": "The Long Confession",
                "language": "en",
                "body": "The confession continued until sunrise.",
                "chapters": [
                    {
                        "ordinal": 1,
                        "title": "Episode 7: The Confession",
                        "body": "The confession continued until sunrise.",
                    }
                ],
                "episodes": [
                    {
                        "ordinal": 1,
                        "title": "Episode 7: The Confession (2/3)",
                        "source_map": [{"chapter_ordinals": [1]}],
                        "estimated_duration_seconds": 480,
                        "metadata": {
                            "text": "The confession continued until sunrise.",
                            "source_heading": "Episode 7: The Confession",
                            "original_title": "Episode 7: The Confession",
                            "source_part_index": 2,
                            "source_part_count": 3,
                            "explicit_source_boundary": True,
                        },
                    }
                ],
            }
        )["novel"]
        split_episode = self.service.novel_for_ui(split["id"])["episodes"][0]
        self.assertEqual(split_episode["original_title"], "Episode 7: The Confession")
        self.assertEqual(split_episode["source_part_index"], 2)
        self.assertEqual(split_episode["source_part_count"], 3)
        self.assertTrue(split_episode["is_source_split"])
        self.assertEqual(split_episode["split_label"], "长集拆段 2/3")
        self.assertEqual(
            split_episode["source_label"],
            "Episode 7: The Confession · 长集拆段 2/3",
        )

    def test_publishing_account_updates_reject_stale_row_version(self) -> None:
        created = self.service.save_publishing_account(
            {
                "platform_id": self.platform.id,
                "name": "US Romance 01",
                "handle": "@story.after.dark",
            }
        )
        self.assertEqual(created["row_version"], 1)

        updated = self.service.save_publishing_account(
            {
                **created,
                "name": "US Romance Lead",
                "expected_version": created["row_version"],
            }
        )
        self.assertEqual(updated["row_version"], 2)

        with self.assertRaises(CatalogConflictError):
            self.service.save_publishing_account(
                {
                    **created,
                    "name": "Stale Edit",
                    "expected_version": created["row_version"],
                }
            )

    def test_import_with_novel_id_creates_a_new_revision_not_a_second_book(self) -> None:
        novel = self._import()
        updated = self.service.import_text(
            {
                "novel_id": novel["id"],
                "title": novel["title"],
                "synopsis": novel["synopsis"],
                "text": (
                    "Chapter 1\nThe phone rang at ten. A stranger whispered my husband's name.\n\n"
                    "Chapter 2\nI drove to the address she gave me. The door was already open.\n\n"
                    "Chapter 3\nA photograph on the wall showed me standing beside her."
                ),
            }
        )

        self.assertTrue(updated["created"])
        self.assertEqual(updated["novel"]["id"], novel["id"])
        self.assertEqual(self.catalog.list_novels(limit=20)["total"], 1)
        self.assertEqual(len(self.catalog.get_novel(novel["id"])["revisions"]), 2)

    def test_binding_code_account_and_unassigned_draft_round_trip(self) -> None:
        novel = self._import()
        bound = self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        self.assertEqual(bound["platform_bindings"][0]["platform_id"], self.platform.id)
        code_result = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "b73165",
            }
        )
        promo = code_result["promo_code"]
        self.assertEqual(promo["value"], "B73165")
        draft_result = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "publishing_account_id": "",
                "episode_ids": [novel["episodes"][0]["id"]],
                "variant_count": 10,
                "approvals": {"main": "pending", "variants": {}},
            }
        )
        self.assertEqual(draft_result["draft"]["variant_count"], 10)
        self.assertEqual(draft_result["draft"]["publishing_account_id"], "")
        self.assertEqual(
            draft_result["draft"]["episode_ids"], [novel["episodes"][0]["id"]]
        )

        account = self.service.save_publishing_account(
            {
                "platform_id": self.platform.id,
                "name": "US Suspense 01",
                "handle": "",
                "region": "US",
                "positioning": "Suspense novels",
            }
        )
        self.assertEqual(account["handle"], "")
        self.assertEqual(account["region"], "US")

    def test_code_rejects_symbols_but_accepts_alphanumeric_values(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        with self.assertRaisesRegex(ValueError, "字母和数字"):
            self.service.add_promo_code(
                {
                    "novel_id": novel["id"],
                    "platform_id": self.platform.id,
                    "code": "B73-165",
                }
            )

    def test_voice_preference_accepts_only_candidates_and_can_change_per_batch(self) -> None:
        novel = self._import()
        stored = self.catalog.get_novel(novel["id"])
        metadata = dict(stored.get("metadata") or {})
        metadata["voice_candidates"] = [
            {
                "provider": "local_kokoro",
                "voice_id": "af_heart",
                "label": "Heart",
            },
            {
                "provider": "local_kokoro",
                "voice_id": "af_bella",
                "label": "Bella",
            },
        ]
        self.catalog.save_novel({"id": novel["id"], "metadata": metadata})

        with self.assertRaisesRegex(ValueError, "3 个试听候选"):
            self.service.lock_voice(
                novel["id"],
                {"provider": "local_kokoro", "voice_id": "af_unknown"},
            )

        self.service.lock_voice(
            novel["id"],
            {"provider": "local_kokoro", "voice_id": "af_heart"},
        )
        changed = self.service.lock_voice(
            novel["id"],
            {
                "provider": "local_kokoro",
                "voice_id": "af_bella",
            },
        )
        self.assertEqual(changed["locked_voice_id"], "af_bella")
        stored = self.catalog.get_novel(novel["id"])
        self.assertEqual(
            stored["metadata"]["preferred_voice_id"],
            "af_bella",
        )
        self.assertNotIn("voice_lock_history", stored["metadata"])

    def test_changing_wpm_keeps_an_already_selected_voice(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "WPMVOICE1",
            }
        )["promo_code"]
        stored = self.catalog.get_novel(novel["id"])
        metadata = dict(stored.get("metadata") or {})
        metadata["voice_candidates"] = [
            {
                "provider": "local_kokoro",
                "voice_id": "af_heart",
                "label": "Heart",
                "profile": "dramatic",
                "narration_wpm": 220,
                "selection_key": "stable-voice-key",
            }
        ]
        self.catalog.save_novel({"id": novel["id"], "metadata": metadata})

        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [novel["episodes"][0]["id"]],
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_heart",
                    "label": "Heart",
                    "profile": "dramatic",
                },
                "production_settings": {"narration_wpm": 280},
            }
        )["draft"]

        self.assertEqual(draft["voice"]["voice_id"], "af_heart")
        self.assertEqual(draft["production_settings"]["narration_wpm"], 280)

    def test_render_jobs_are_direct_full_jobs_and_keep_one_voice(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "B73165",
            }
        )["promo_code"]
        self.service.lock_voice(
            novel["id"],
            {
                "provider": "local_kokoro",
                "voice_id": "af_heart",
                "label": "American suspense",
            },
        )
        current = self.catalog.get_novel(novel["id"])
        first, second = current["current_revision"]["episodes"][:2]
        large = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [first["id"], second["id"]],
                "target_video_count": 21,
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_bella",
                },
            }
        )["draft"]
        self.assertEqual(large["target_video_count"], 21)
        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [first["id"], second["id"]],
                "variant_count": 2,
            }
        )["draft"]
        video = self.root / "video"
        music = self.root / "music"
        output = self.root / "output"
        video.mkdir()
        music.mkdir()

        saved, platform_id, jobs = self.service.build_render_jobs(
            {
                "draft_id": draft["id"],
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(output),
            }
        )

        self.assertEqual(platform_id, self.platform.id)
        self.assertEqual(saved["status"], "ready")
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(item.status == JobStatus.QUEUED for item in jobs))
        self.assertTrue(all(item.job_kind == "full" for item in jobs))
        self.assertTrue(all(not item.preview_approved for item in jobs))
        self.assertTrue(
            all(item.stage_label == "等待生成完整视频与配音" for item in jobs)
        )
        self.assertTrue(all(item.locked_voice_id == "af_heart" for item in jobs))
        self.assertTrue(all(item.variant_count == 2 for item in jobs))
        self.assertTrue(all(item.episode_count == 2 for item in jobs))
        self.assertTrue(all(item.is_final_episode for item in jobs))
        self.assertEqual(len({item.variant_seed for item in jobs}), 2)
        self.assertTrue(all(item.episode_id == first["id"] for item in jobs))
        self.assertTrue(
            all(item.episode_ids == (first["id"], second["id"]) for item in jobs)
        )
        self.assertTrue(all(item.episode_label == "E001-E002" for item in jobs))
        self.assertEqual(len({item.source_file for item in jobs}), 1)
        merged_text = Path(jobs[0].source_file).read_text(encoding="utf-8")
        self.assertFalse(merged_text.startswith("Previously,"))
        self.assertIn("Chapter 1", merged_text)
        self.assertIn("Chapter 2", merged_text)
        analysis = analyze_manuscript(merged_text, jobs[0].source_file)
        self.assertNotIn("Chapter 1", analysis.narration_text)
        self.assertNotIn("Chapter 2", analysis.subtitle_text)
        self.assertAlmostEqual(analysis.statistics.chapter_pause_seconds, 0.8)

    def test_large_render_plan_is_disk_backed_and_iterated_incrementally(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "BIGPLAN",
            }
        )["promo_code"]
        self.service.lock_voice(
            novel["id"],
            {"provider": "local_kokoro", "voice_id": "af_heart"},
        )
        episode = self.catalog.get_novel(novel["id"])["current_revision"]["episodes"][0]
        uncapped = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [episode["id"]],
                "target_video_count": 1_000_001,
            }
        )["draft"]
        self.assertEqual(uncapped["target_video_count"], 1_000_001)
        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [episode["id"]],
                "target_video_count": 1001,
            }
        )["draft"]
        video = self.root / "large-video"
        music = self.root / "large-music"
        output = self.root / "large-output"
        video.mkdir()
        music.mkdir()

        _saved, _platform_id, total, iterator = self.service.build_render_job_plan(
            {
                "draft_id": draft["id"],
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(output),
            }
        )

        self.assertEqual(total, 1001)
        self.assertNotIsInstance(iterator, (list, tuple))
        self.assertEqual(
            [job.variant_index for job in islice(iterator, 3)],
            [1, 2, 3],
        )
        iterator.close()
        self.assertEqual(list((self.root / "data" / "job-spool").glob("*.jsonl")), [])

    def test_build_migrates_retired_legacy_voice_before_queue_and_records_it(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "VOICEOLD1",
            }
        )["promo_code"]
        stored = self.catalog.get_novel(novel["id"])
        metadata = dict(stored.get("metadata") or {})
        metadata["voice_candidates"] = [
            {
                "provider": "kokoro",
                "voice_id": "af_retired",
                "label": "Legacy warm voice",
                "profile": "warm",
            }
        ]
        self.catalog.save_novel({"id": novel["id"], "metadata": metadata})
        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [novel["episodes"][0]["id"]],
                "voice": {
                    "provider": "kokoro",
                    "voice_id": "af_retired",
                    "label": "Legacy warm voice",
                    "profile": "warm",
                },
            }
        )["draft"]

        def rebuilt_candidates(_text, _mood, _output, *, language="en"):
            self.assertEqual(language, "en")
            return [
                {
                    "provider": "local_kokoro",
                    "voice_id": "af_bella",
                    "label": "Bella",
                    "profile": "warm",
                    "audio_path": "preview.wav",
                    "audio_uri": "file:///preview.wav",
                    "duration_seconds": 1.0,
                    "excerpt": "The phone rang at midnight.",
                    "language": "en",
                    "voice_name": "Bella",
                }
            ]

        self.service.voice_previews.generate = rebuilt_candidates
        video = self.root / "legacy-video"
        music = self.root / "legacy-music"
        output = self.root / "legacy-output"
        video.mkdir()
        music.mkdir()
        saved, _platform_id, jobs = self.service.build_render_jobs(
            {
                "draft_id": draft["id"],
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(output),
            }
        )

        self.assertTrue(jobs)
        self.assertTrue(all(job.locked_voice_id == "af_bella" for job in jobs))
        self.assertTrue(
            all(job.locked_voice_provider == "local_kokoro" for job in jobs)
        )
        self.assertEqual(saved["metadata"]["voice"]["voice_id"], "af_bella")
        migration = saved["metadata"]["voice_migrations"][-1]
        self.assertEqual(migration["from"]["voice_id"], "af_retired")
        self.assertEqual(migration["to"]["voice_id"], "af_bella")
        self.assertEqual(migration["reason"], "legacy_voice_replaced_same_profile")
        # Candidate rebuilding during render preflight is draft-local.  It
        # must not silently replace the shared novel shortlist.
        shared_metadata = dict(
            self.catalog.get_novel(novel["id"]).get("metadata") or {}
        )
        self.assertEqual(
            shared_metadata["voice_candidates"][0]["voice_id"],
            "af_retired",
        )

    def test_draft_voice_preflight_does_not_overwrite_concurrent_shared_lock(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "VOICERACE",
            }
        )["promo_code"]
        self.catalog.save_novel_voice_state(
            novel["id"],
            {
                "locked_voice_provider": "kokoro",
                "locked_voice_id": "af_retired",
                "locked_voice_label": "Legacy warm voice",
                "locked_voice_profile": "warm",
                "voice_candidates": [
                    {
                        "provider": "kokoro",
                        "voice_id": "af_retired",
                        "label": "Legacy warm voice",
                        "profile": "warm",
                    }
                ],
                "voice_lock_history": [{"voice_id": "before-render"}],
            },
        )
        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [novel["episodes"][0]["id"]],
                "voice": {
                    "provider": "kokoro",
                    "voice_id": "af_retired",
                    "label": "Legacy warm voice",
                    "profile": "warm",
                },
            }
        )["draft"]

        def rebuilt_candidates(_text, _mood, _output, *, language="en"):
            self.assertEqual(language, "en")
            # Simulate an administrator changing the shared series voice on a
            # second computer while this worker is preflighting its old draft.
            self.catalog.save_novel_voice_state(
                novel["id"],
                {
                    "locked_voice_provider": "local_kokoro",
                    "locked_voice_id": "af_sarah",
                    "locked_voice_label": "Sarah",
                    "locked_voice_profile": "confident",
                    "voice_candidates": [
                        {
                            "provider": "local_kokoro",
                            "voice_id": "af_sarah",
                            "label": "Sarah",
                            "profile": "confident",
                        }
                    ],
                    "voice_lock_history": [{"voice_id": "admin-change"}],
                },
            )
            return [
                {
                    "provider": "local_kokoro",
                    "voice_id": "af_bella",
                    "label": "Bella",
                    "profile": "warm",
                    "audio_path": "preview.wav",
                    "audio_uri": "file:///preview.wav",
                    "duration_seconds": 1.0,
                    "excerpt": "The phone rang at midnight.",
                    "language": "en",
                    "voice_name": "Bella",
                }
            ]

        self.service.voice_previews.generate = rebuilt_candidates
        video = self.root / "race-video"
        music = self.root / "race-music"
        output = self.root / "race-output"
        video.mkdir()
        music.mkdir()
        saved, _platform_id, jobs = self.service.build_render_jobs(
            {
                "draft_id": draft["id"],
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(output),
            }
        )

        self.assertTrue(jobs)
        self.assertTrue(all(job.locked_voice_id == "af_bella" for job in jobs))
        self.assertEqual(saved["metadata"]["voice"]["voice_id"], "af_bella")
        self.assertEqual(
            saved["metadata"]["voice_migrations"][-1]["to"]["voice_id"],
            "af_bella",
        )
        shared_metadata = dict(
            self.catalog.get_novel(novel["id"]).get("metadata") or {}
        )
        self.assertEqual(shared_metadata["locked_voice_id"], "af_sarah")
        self.assertEqual(shared_metadata["locked_voice_profile"], "confident")
        self.assertEqual(
            shared_metadata["voice_candidates"][0]["voice_id"],
            "af_sarah",
        )
        self.assertEqual(
            [item["voice_id"] for item in shared_metadata["voice_lock_history"]],
            ["admin-change"],
        )

    def test_non_contiguous_selection_uses_true_previous_episode_and_story_order(self) -> None:
        novel = self.service.import_text(
            {
                "title": "Four Doors",
                "language": "en",
                "text": (
                    "Chapter 1 - The Silver Key\n"
                    "At sunset, Ava hid the silver key beneath the hallway clock.\n\n"
                    "Chapter 2 - The Empty Room\n"
                    "At midnight, Ava opened the empty room and found a torn blue coat.\n\n"
                    "Chapter 3 - The Red Ledger\n"
                    "Before dawn, Mara learned the red ledger named Victor as the thief.\n\n"
                    "Chapter 4 - The Knock\n"
                    "When morning came, three knocks sounded from inside the locked wall."
                ),
            }
        )["novel"]
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "ORDER4",
            }
        )["promo_code"]
        episodes = novel["episodes"]
        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                # Deliberately click in reverse/non-contiguous order. The batch
                # must still render E001, E002, E004 in manuscript order.
                "episode_ids": [
                    episodes[3]["id"],
                    episodes[1]["id"],
                    episodes[0]["id"],
                ],
                "target_video_count": 3,
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_heart",
                },
            }
        )["draft"]
        video = self.root / "ordered-video"
        music = self.root / "ordered-music"
        output = self.root / "ordered-output"
        video.mkdir()
        music.mkdir()

        _, _, jobs = self.service.build_render_jobs(
            {
                "draft_id": draft["id"],
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(output),
            }
        )

        self.assertEqual(len(jobs), 3)
        self.assertEqual([job.episode_number for job in jobs], [1, 1, 1])
        self.assertEqual([job.episode_count for job in jobs], [4, 4, 4])
        self.assertTrue(all(job.is_final_episode for job in jobs))
        self.assertTrue(
            all(
                job.episode_ids
                == (episodes[0]["id"], episodes[1]["id"], episodes[3]["id"])
                for job in jobs
            )
        )
        self.assertTrue(
            all(job.episode_label == "E001_E002_E004" for job in jobs)
        )
        self.assertEqual(len({job.source_file for job in jobs}), 1)
        merged_text = Path(jobs[0].source_file).read_text(encoding="utf-8")
        self.assertFalse(merged_text.startswith("Previously,"))
        self.assertIn("silver key", merged_text)
        self.assertIn("torn blue coat", merged_text)
        self.assertIn("three knocks", merged_text)
        self.assertNotIn("red ledger", merged_text)
        merged_analysis = analyze_manuscript(merged_text, jobs[0].source_file)
        self.assertAlmostEqual(
            merged_analysis.statistics.chapter_pause_seconds,
            1.6,
        )

        # Selecting only an intermediate episode must not turn the last item in
        # this batch into a finale. Finale status comes from the novel's full
        # current revision (four episodes), not from the selection boundary.
        middle_only = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [episodes[1]["id"]],
                "target_video_count": 1,
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_heart",
                },
            }
        )["draft"]
        _, _, middle_jobs = self.service.build_render_jobs(
            {
                "draft_id": middle_only["id"],
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(self.root / "middle-only-output"),
            }
        )
        self.assertEqual(len(middle_jobs), 1)
        self.assertEqual(middle_jobs[0].episode_number, 2)
        self.assertEqual(middle_jobs[0].episode_count, 4)
        self.assertFalse(middle_jobs[0].is_final_episode)
        middle_text = Path(middle_jobs[0].source_file).read_text(encoding="utf-8")
        self.assertTrue(middle_text.startswith("Previously,"))
        self.assertEqual(middle_text.count("Previously,"), 1)
        middle_analysis = analyze_manuscript(
            middle_text, middle_jobs[0].source_file
        )
        self.assertNotIn("Chapter 2", middle_analysis.narration_text)
        self.assertAlmostEqual(
            middle_analysis.statistics.chapter_pause_seconds,
            0.8,
        )

        later_group = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [episodes[2]["id"], episodes[3]["id"]],
                "target_video_count": 1,
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_heart",
                },
            }
        )["draft"]
        _, _, later_jobs = self.service.build_render_jobs(
            {
                "draft_id": later_group["id"],
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(self.root / "later-group-output"),
            }
        )
        self.assertEqual(len(later_jobs), 1)
        self.assertEqual(later_jobs[0].episode_label, "E003-E004")
        later_text = Path(later_jobs[0].source_file).read_text(encoding="utf-8")
        self.assertTrue(later_text.startswith("Previously,"))
        self.assertEqual(later_text.count("Previously,"), 1)
        self.assertIn("torn blue coat", later_text)
        self.assertIn("red ledger", later_text)
        self.assertIn("three knocks", later_text)
        later_analysis = analyze_manuscript(
            later_text, later_jobs[0].source_file
        )
        self.assertAlmostEqual(
            later_analysis.statistics.chapter_pause_seconds,
            1.6,
        )

    def test_batch_voice_recipe_and_total_count_are_isolated_from_the_novel(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "B73165",
            }
        )["promo_code"]
        current = self.catalog.get_novel(novel["id"])
        first, second = current["current_revision"]["episodes"][:2]
        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [first["id"], second["id"]],
                "target_video_count": 5,
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_bella",
                    "label": "Maya · Intimate reveal",
                    "profile": "warm",
                },
                "production_settings": {
                    "narration_wpm": 228,
                    "output_fps": 30,
                    "bgm_volume": 0.34,
                    "subtitle_preset": "cinematic_shadow",
                    "subtitle": {"font_size": 58, "max_chars_per_line": 26},
                },
            }
        )["draft"]

        self.assertEqual(draft["target_video_count"], 5)
        self.assertEqual(draft["voice"]["voice_id"], "af_bella")
        self.assertEqual(draft["production_settings"]["narration_wpm"], 228)
        self.assertGreaterEqual(draft["row_version"], 1)
        self.assertFalse(self.catalog.get_novel(novel["id"])["metadata"].get("locked_voice_id"))

        video = self.root / "batch-video"
        music = self.root / "batch-music"
        output = self.root / "batch-output"
        video.mkdir()
        music.mkdir()
        _, _, jobs = self.service.build_render_jobs(
            {
                "draft_id": draft["id"],
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(output),
            }
        )

        self.assertEqual(len(jobs), 5)
        self.assertEqual([job.variant_count for job in jobs], [5, 5, 5, 5, 5])
        self.assertTrue(
            all(job.episode_ids == (first["id"], second["id"]) for job in jobs)
        )
        self.assertTrue(all(job.episode_label == "E001-E002" for job in jobs))
        self.assertEqual(len({job.source_file for job in jobs}), 1)
        self.assertTrue(all(job.locked_voice_id == "af_bella" for job in jobs))
        self.assertTrue(all(job.settings_snapshot["narration_wpm"] == 228 for job in jobs))
        self.assertTrue(all(job.settings_snapshot["output_fps"] == 30 for job in jobs))
        self.assertTrue(all(job.settings_snapshot["bgm_volume"] == 0.34 for job in jobs))
        self.assertTrue(
            all(job.settings_snapshot["subtitle"]["font_size"] == 58 for job in jobs)
        )

    def test_new_draft_defaults_to_last_successful_voice_but_can_replace_it(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "VOICE01",
            }
        )["promo_code"]
        base = {
            "novel_id": novel["id"],
            "platform_id": self.platform.id,
            "promo_code_id": promo["id"],
            "episode_ids": [novel["episodes"][0]["id"]],
            "target_video_count": 1,
        }
        first = self.service.save_draft(
            {
                **base,
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_heart",
                    "label": "Heart",
                    "profile": "dramatic",
                },
            }
        )["draft"]
        self.catalog.save_production_record(
            {
                "draft_id": first["id"],
                "job_id": "voice-success",
                "status": "completed",
                "metadata": {
                    "job_snapshot": {
                        "locked_voice_provider": "local_kokoro",
                        "locked_voice_id": "af_heart",
                    }
                },
            }
        )

        inherited = self.service.save_draft(base)["draft"]
        changed = self.service.save_draft(
            {
                **base,
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_bella",
                    "profile": "warm",
                },
            }
        )["draft"]

        self.assertEqual(inherited["voice"]["voice_id"], "af_heart")
        self.assertEqual(changed["voice"]["voice_id"], "af_bella")
        projected = self.service.novel_for_ui(novel["id"])
        self.assertEqual(projected["locked_voice_id"], "af_heart")

    def test_reuse_audio_recipe_and_new_video_controls_are_frozen(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "REUSE01",
            }
        )["promo_code"]
        narration = self.root / "existing-storyforge-video.mp4"
        narration.write_bytes(b"storyforge video")
        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [novel["episodes"][0]["id"]],
                "target_video_count": 1,
                "production_settings": {
                    "output_mode": "reuse_audio",
                    "narration_wpm": 280,
                    "video_playback_speed": 2.5,
                    "video_transition": "fade",
                    "subtitle_word_mode": "cumulative",
                    "bgm_mode": "auto",
                },
                "source_narration_audio": str(narration),
            }
        )["draft"]
        video = self.root / "reuse-video"
        video.mkdir()
        _, _, jobs = self.service.build_render_jobs(
            {
                "draft_id": draft["id"],
                "video_folder": str(video),
                "music_folder": "",
                "output_folder": str(self.root / "reuse-output"),
            }
        )

        snapshot = jobs[0].settings_snapshot
        self.assertEqual(draft["source_narration_audio"], str(narration))
        self.assertEqual(snapshot["output_mode"], "reuse_audio")
        self.assertEqual(snapshot["source_narration_audio"], str(narration))
        self.assertEqual(snapshot["narration_wpm"], 280)
        self.assertEqual(snapshot["video_playback_speed"], 2.5)
        self.assertEqual(snapshot["video_transition"], "fade")
        self.assertEqual(snapshot["subtitle_word_mode"], "cumulative")
        # A StoryForge video already contains its complete audio mix. Reuse
        # must not require a music folder or add a second BGM track.
        self.assertEqual(snapshot["bgm_mode"], "none")
        self.assertEqual(jobs[0].music_folder, "")

    def test_reuse_audio_rejects_unsupported_external_video_container(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "REUSE02",
            }
        )["promo_code"]
        external = self.root / "ordinary-video.avi"
        external.write_bytes(b"not a StoryForge source")
        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [novel["episodes"][0]["id"]],
                "target_video_count": 1,
                "production_settings": {
                    "output_mode": "reuse_audio",
                    "bgm_mode": "none",
                },
                "source_narration_audio": str(external),
            }
        )["draft"]
        video = self.root / "reuse-video"
        video.mkdir()

        with self.assertRaisesRegex(ValueError, "MP3.*MP4/MOV/MKV/WEBM"):
            self.service.build_render_jobs(
                {
                    "draft_id": draft["id"],
                    "video_folder": str(video),
                    "music_folder": "",
                    "output_folder": str(self.root / "reuse-output"),
                }
            )

    def test_audio_only_plan_requires_output_but_not_video_or_music_folders(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "AUDIO01",
            }
        )["promo_code"]
        current = self.catalog.get_novel(novel["id"])
        episode = current["current_revision"]["episodes"][0]
        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [episode["id"]],
                "target_video_count": 2,
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_bella",
                    "profile": "dramatic",
                },
                "production_settings": {"output_mode": "audio_only"},
            }
        )["draft"]

        output = self.root / "audio-only-output"
        _, _, jobs = self.service.build_render_jobs(
            {
                "draft_id": draft["id"],
                "video_folder": "",
                "music_folder": "",
                "output_folder": str(output),
            }
        )

        self.assertTrue(output.is_dir())
        # Audio-only is one reusable narration for the merged selection; the
        # video variant count must not duplicate identical TTS work.
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].batch_total_count, 1)
        self.assertEqual(jobs[0].variant_count, 1)
        self.assertTrue(all(job.video_folder == "" for job in jobs))
        self.assertTrue(all(job.music_folder == "" for job in jobs))
        self.assertTrue(all(job.settings_snapshot["output_mode"] == "audio_only" for job in jobs))
        self.assertTrue(all("配音" in job.stage_label for job in jobs))

    def test_render_plan_rejects_episode_groups_from_mixed_revisions(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "REVISION",
            }
        )["promo_code"]
        self.service.lock_voice(
            novel["id"],
            {"provider": "local_kokoro", "voice_id": "af_heart"},
        )
        old_episode_id = novel["episodes"][0]["id"]
        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [old_episode_id],
                "target_video_count": 1,
            }
        )["draft"]
        revised = self.service.import_text(
            {
                "novel_id": novel["id"],
                "title": novel["title"],
                "text": (
                    "Chapter 1\nThe revised phone rang just before midnight.\n\n"
                    "Chapter 2\nThe revised door stood open beneath a red light."
                ),
            }
        )["novel"]
        new_episode_id = revised["episodes"][0]["id"]
        stored = self.catalog.get_draft(draft["id"])
        self.catalog.save_draft(
            {
                "id": draft["id"],
                "episode_ids": [old_episode_id, new_episode_id],
                "metadata": stored["metadata"],
            }
        )
        video = self.root / "mixed-revision-video"
        music = self.root / "mixed-revision-music"
        video.mkdir()
        music.mkdir()

        with self.assertRaisesRegex(ValueError, "同一个小说版本"):
            self.service.build_render_jobs(
                {
                    "draft_id": draft["id"],
                    "video_folder": str(video),
                    "music_folder": str(music),
                    "output_folder": str(self.root / "mixed-revision-output"),
                }
            )

    def test_render_jobs_freeze_batch_story_mood_intro_card_and_video_template(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "MOODCARD",
            }
        )["promo_code"]
        self.settings.video_template = "platform_story_card"
        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [novel["episodes"][0]["id"]],
                "target_video_count": 2,
                "story_mood": "romance",
                "story_mood_source": "manual",
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_bella",
                },
            }
        )["draft"]

        # Live defaults can change after the draft is saved; queued jobs retain
        # the approved batch recipe.
        self.settings.video_template = "classic"
        video = self.root / "mood-video"
        music = self.root / "mood-music"
        output = self.root / "mood-output"
        video.mkdir()
        music.mkdir()
        _, _, jobs = self.service.build_render_jobs(
            {
                "draft_id": draft["id"],
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(output),
            }
        )

        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(job.story_mood == "romance" for job in jobs))
        self.assertTrue(all(job.story_mood_source == "manual" for job in jobs))
        self.assertTrue(
            all(job.intro_card_text == novel["synopsis"] for job in jobs)
        )
        self.assertTrue(
            all(job.intro_card_source == "novel_synopsis" for job in jobs)
        )
        self.assertTrue(
            all(
                job.settings_snapshot["video_template"] == "platform_story_card"
                for job in jobs
            )
        )

    def test_story_card_ai_copy_is_frozen_before_queue_and_flows_into_every_variant(self) -> None:
        self.settings.video_template = "platform_story_card"
        self.settings.providers.text_provider = "groq"
        requests = []

        class IntroProvider:
            def polish(self, request) -> TextResult:
                requests.append(request)
                return TextResult(
                    polished_text=(
                        "Her husband's secret is no longer safe—a stranger already knows it."
                    ),
                    hook="A stranger knows his secret.",
                    ending_cta="Keep reading.",
                    mood="suspense",
                    provider="groq",
                    model="test-model",
                )

        service = LibraryService(
            self.catalog,
            lambda: self.settings,
            self.root / "ai-intro-data",
            text_provider_factory=lambda _config: IntroProvider(),
        )
        novel = self._import()
        service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "AICARD",
            }
        )["promo_code"]
        intro = service.generate_intro_card_copy(
            novel["id"],
            [novel["episodes"][0]["id"]],
        )
        draft = service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [novel["episodes"][0]["id"]],
                "target_video_count": 2,
                "intro_card_text": intro["text"],
                "intro_card_source": intro["source"],
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_bella",
                },
            }
        )["draft"]
        video = self.root / "ai-intro-video"
        music = self.root / "ai-intro-music"
        output = self.root / "ai-intro-output"
        video.mkdir()
        music.mkdir()
        _, _, jobs = service.build_render_jobs(
            {
                "draft_id": draft["id"],
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(output),
                "confirm_language": True,
            }
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].purpose, "intro_card")
        self.assertEqual(
            requests[0].text, "A stranger knows her husband's secret."
        )
        self.assertTrue(
            all(
                job.intro_card_text
                == "Her husband's secret is no longer safe—a stranger already knows it."
                for job in jobs
            )
        )
        self.assertTrue(
            all(job.intro_card_source == "novel_synopsis_ai" for job in jobs)
        )

    def test_story_card_copy_falls_back_to_two_body_sentences_when_ai_fails(self) -> None:
        self.settings.providers.text_provider = "groq"

        class FailingProvider:
            def polish(self, _request) -> TextResult:
                raise ProviderConfigurationError(
                    "missing API key", provider="groq"
                )

        service = LibraryService(
            self.catalog,
            lambda: self.settings,
            self.root / "failed-intro-data",
            text_provider_factory=lambda _config: FailingProvider(),
        )
        copy, source = service._intro_card_copy(
            "",
            "The phone rang at ten. A stranger whispered his name. Third sentence is hidden.",
            title="The Midnight Call",
            language="English",
        )

        self.assertEqual(
            copy, "The phone rang at ten. A stranger whispered his name."
        )
        self.assertEqual(source, "episode_excerpt")
        self.assertNotIn("Third sentence", copy)
        long_copy, _ = service._intro_card_copy(
            " ".join(f"storyword{index}" for index in range(70)) + ".",
            "Unused episode text.",
            title="Long Synopsis",
            language="English",
        )
        self.assertLessEqual(len(long_copy.removesuffix("…").split()), 28)
        self.assertLessEqual(len(long_copy), 155)
        self.assertTrue(long_copy.endswith("…"))

    def test_intro_card_body_fallback_uses_all_selected_episodes_in_story_order(self) -> None:
        self.settings.providers.text_provider = "groq"
        requests = []

        class IntroProvider:
            def polish(self, request) -> TextResult:
                requests.append(request)
                return TextResult(
                    polished_text=(
                        "The phone rang at ten, and the door at the address was already open."
                    ),
                    hook="The phone rang.",
                    ending_cta="Keep reading.",
                    mood="suspense",
                    provider="groq",
                    model="test-model",
                )

        service = LibraryService(
            self.catalog,
            lambda: self.settings,
            self.root / "multi-episode-intro-data",
            text_provider_factory=lambda _config: IntroProvider(),
        )
        novel = self._import()
        self.catalog.save_novel({"id": novel["id"], "synopsis": ""})
        episodes = novel["episodes"][:2]

        result = service.generate_intro_card_copy(
            novel["id"],
            [episodes[1]["id"], episodes[0]["id"]],
        )

        self.assertTrue(result["text"])
        self.assertEqual(len(requests), 1)
        prompt = requests[0].text
        self.assertIn("phone rang at ten", prompt)
        self.assertIn("door was already open", prompt)
        self.assertLess(
            prompt.index("phone rang at ten"),
            prompt.index("door was already open"),
        )

    def test_intro_copy_budget_fits_the_real_five_line_card_without_second_truncation(self) -> None:
        english = _fit_intro_card_text(
            " ".join(f"betrayalword{index}" for index in range(60)) + "."
        )
        cjk = _fit_intro_card_text(
            "她在结婚纪念日发现丈夫隐藏多年的秘密，一张陌生照片让她开始怀疑身边的每一个人。"
            * 3
        )

        self.assertLessEqual(len(english.removesuffix("…").split()), 28)
        self.assertLessEqual(len(english), 155)
        self.assertLessEqual(len(cjk), 70)
        rendered_english = _fit_card_lines(english, width=35, max_lines=5)
        self.assertEqual(
            " ".join(rendered_english.replace(r"\N", " ").split()),
            " ".join(english.split()),
        )
        rendered_cjk = _fit_card_lines(cjk, width=35, max_lines=5)
        self.assertEqual(
            rendered_cjk.replace(r"\N", "").replace(" ", ""),
            cjk.replace(" ", ""),
        )

    def test_story_card_copy_rejects_ungrounded_ai_events(self) -> None:
        self.settings.providers.text_provider = "groq"

        class HallucinatingProvider:
            def polish(self, _request) -> TextResult:
                return TextResult(
                    polished_text="Max murders Eve after stealing a million dollars.",
                    hook="Murder changes everything.",
                    ending_cta="Keep reading.",
                    mood="suspense",
                    provider="groq",
                )

        service = LibraryService(
            self.catalog,
            lambda: self.settings,
            self.root / "hallucination-intro-data",
            text_provider_factory=lambda _config: HallucinatingProvider(),
        )
        copy, source = service._intro_card_copy(
            "A stranger knows her husband's secret.",
            "The phone rang at ten.",
            title="The Midnight Call",
            language="English",
        )

        self.assertEqual(copy, "A stranger knows her husband's secret.")
        self.assertEqual(source, "novel_synopsis")

    def test_draft_rejects_inactive_codes_and_accounts_from_another_platform(self) -> None:
        novel = self._import()
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "B73165",
            }
        )["promo_code"]
        base = {
            "novel_id": novel["id"],
            "platform_id": self.platform.id,
            "promo_code_id": promo["id"],
            "episode_ids": [novel["episodes"][0]["id"]],
            "target_video_count": 1,
            "voice": {"provider": "local_kokoro", "voice_id": "af_heart"},
        }
        self.service.update_promo_code(
            {
                "novel_id": novel["id"],
                "promo_code_id": promo["id"],
                "active": False,
            }
        )
        with self.assertRaisesRegex(ValueError, "有效口令"):
            self.service.save_draft(base)

        self.service.update_promo_code(
            {
                "novel_id": novel["id"],
                "promo_code_id": promo["id"],
                "active": True,
            }
        )
        other = PlatformProfile(id="motonovel", name="MotoNovel")
        self.service.sync_platforms([other])
        account = self.service.save_publishing_account(
            {
                "platform_id": other.id,
                "name": "Moto Account",
                "handle": "@moto.account",
                "active": True,
            }
        )
        with self.assertRaisesRegex(ValueError, "不属于当前小说平台"):
            self.service.save_draft(
                {**base, "publishing_account_id": account["id"]}
            )

    def test_spanish_novel_routes_local_kokoro_preview_and_render_voice(self) -> None:
        novel = self.service.import_text(
            {
                "title": "A neutral title",
                "language": "auto",
                "text": (
                    "Anoche sonó el teléfono cuando estaba a punto de dormir. La mujer "
                    "dijo que conocía a mi marido y preguntó por qué nunca había visto la "
                    "habitación cerrada. Pensé que estaba mintiendo, pero después miré "
                    "dentro del escritorio y todo cambió."
                ),
            }
        )["novel"]
        self.assertEqual(novel["language"], "es")
        preview_calls: list[dict[str, object]] = []

        def generate_preview(_text, _mood, _output, *, language="en"):
            preview_calls.append({"language": language})
            return [
                {
                    "profile": "warm",
                    "label": "温暖亲密",
                    "provider": "local_kokoro",
                    "voice_id": "ef_dora",
                    "audio_path": "preview.wav",
                    "audio_uri": "file:///preview.wav",
                    "duration_seconds": 1.0,
                    "excerpt": "Anoche sonó el teléfono.",
                    "language": "es",
                    "voice_name": "Dora",
                }
            ]

        self.service.voice_previews.generate = generate_preview
        preview = self.service.generate_voice_candidates(novel["id"])
        self.assertEqual(preview_calls, [{"language": "es"}])
        self.assertEqual(preview["candidates"][0]["voice_id"], "ef_dora")

        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "SPANISH1",
            }
        )["promo_code"]
        video = self.root / "video"
        music = self.root / "music"
        output = self.root / "output"
        for folder in (video, music, output):
            folder.mkdir()
        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [novel["episodes"][0]["id"]],
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "ef_dora",
                },
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(output),
            }
        )["draft"]
        self.assertEqual(draft["production_settings"]["language"], "es")
        _saved, _platform_id, jobs = self.service.build_render_jobs(
            {"draft_id": draft["id"]}
        )
        self.assertTrue(jobs)

    def test_unknown_language_requires_confirmation_before_build(self) -> None:
        imported = self.catalog.import_novel(
            {
                "title": "Short manuscript",
                "body": "A locked door. A midnight call.",
                "episodes": [
                    {
                        "ordinal": 1,
                        "metadata": {"text": "A locked door. A midnight call."},
                    }
                ],
            }
        )["novel"]
        novel = self.service.novel_for_ui(imported["id"])
        self.assertEqual(novel["language"], "unknown")
        with self.assertRaisesRegex(ValueError, "语种尚未可靠确认"):
            self.service.generate_voice_candidates(novel["id"])
        self.service.save_binding(
            {"novel_id": novel["id"], "platform_id": self.platform.id}
        )
        promo = self.service.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "code": "UNKNOWN1",
            }
        )["promo_code"]
        draft = self.service.save_draft(
            {
                "novel_id": novel["id"],
                "platform_id": self.platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [novel["episodes"][0]["id"]],
                "voice": {
                    "provider": "local_kokoro",
                    "voice_id": "af_heart",
                },
            }
        )["draft"]
        with self.assertRaisesRegex(ValueError, "语种尚未可靠确认") as caught:
            self.service.build_render_jobs({"draft_id": draft["id"]})
        self.assertIn("完整视频", str(caught.exception))
        self.assertNotIn("样片", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
