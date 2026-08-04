from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from storyforge.catalog import CatalogRepository
from storyforge.production_presets import (
    CURATED_PRODUCTION_PRESETS,
    PRESET_SCHEMA_VERSION,
    ProductionPresetStore,
    validate_production_preset,
)


class ProductionPresetTests(unittest.TestCase):
    def test_schema_four_personal_presets_migrate_retired_subtitle_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "production-presets.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 4,
                        "presets": [
                            {
                                "id": "employee_word",
                                "name": "Employee word captions",
                                "owner_user_id": "employee-1",
                                "revision": 7,
                                "recipe": {
                                    "production_settings": {
                                        "subtitle_preset": "word_pop_sync",
                                        "subtitle_word_mode": "off",
                                        "subtitle": {"active_color": "#12ABEF"},
                                    }
                                },
                            },
                            {
                                "id": "employee_minimal",
                                "name": "Employee minimal captions",
                                "owner_user_id": "employee-1",
                                "revision": 3,
                                "recipe": {
                                    "production_settings": {
                                        "subtitle_preset": "minimal_bottom",
                                        "subtitle": {"bottom_margin": 275},
                                    }
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = ProductionPresetStore(path)

            presets = {
                item["id"]: item
                for item in store.list(
                    viewer_user_id="employee-1",
                    can_manage_all=False,
                )
            }

            self.assertEqual(PRESET_SCHEMA_VERSION, 5)
            self.assertEqual(set(presets), {"employee_word", "employee_minimal"})
            word = presets["employee_word"]
            minimal = presets["employee_minimal"]
            self.assertEqual(word["owner_user_id"], "employee-1")
            self.assertEqual(word["revision"], 7)
            self.assertEqual(
                word["recipe"]["production_settings"]["subtitle_preset"],
                "clear_outline",
            )
            self.assertEqual(
                word["recipe"]["production_settings"]["subtitle_word_mode"],
                "single",
            )
            self.assertEqual(
                word["recipe"]["production_settings"]["subtitle"]["active_color"],
                "#12ABEF",
            )
            self.assertEqual(
                minimal["recipe"]["production_settings"]["subtitle_preset"],
                "clear_outline",
            )
            self.assertEqual(
                minimal["recipe"]["production_settings"]["subtitle"]["bottom_margin"],
                275,
            )

    def test_schema_three_ownerless_presets_are_preserved_but_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "production-presets.json"
            path.write_text(
                '{"schema_version":3,"presets":[{"id":"legacy_three",'
                '"name":"Legacy three","recipe":{"production_settings":'
                '{"narration_wpm":190}}}]}',
                encoding="utf-8",
            )
            store = ProductionPresetStore(path)
            presets = store.list()
            persisted = store._read_overrides()

        self.assertEqual(presets, [])
        legacy = persisted["legacy_three"]
        self.assertEqual(
            legacy["recipe"]["production_settings"]["narration_wpm"], 200
        )

    def test_bundled_presets_are_not_injected_for_any_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = ProductionPresetStore(Path(temp) / "production-presets.json")
            presets = store.list(can_manage_all=True)
            employee_view = store.list(
                viewer_user_id="employee-1", can_manage_all=False
            )

        self.assertEqual(presets, [])
        self.assertEqual(employee_view, [])

    def test_retired_bundled_preset_ids_cannot_be_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = ProductionPresetStore(Path(temp) / "production-presets.json")
            original = CURATED_PRODUCTION_PRESETS[0]
            with self.assertRaisesRegex(PermissionError, "已停用"):
                store.save(original, updated_by="admin-1", can_manage_all=True)
            self.assertEqual(store.list(), [])

    def test_custom_preset_has_no_artificial_video_count_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = ProductionPresetStore(Path(temp) / "production-presets.json")
            saved = store.save(
                {
                    "name": "大批量",
                    "recipe": {
                        "target_video_count": 1001,
                        "production_settings": {"output_fps": 60},
                    },
                }
            )
            self.assertEqual(saved["recipe"]["target_video_count"], 1001)
            self.assertFalse(saved["curated"])

    def test_target_video_count_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            validate_production_preset(
                {
                    "name": "invalid count",
                    "recipe": {"target_video_count": -1},
                }
            )

    def test_shared_preset_has_revision_hash_and_actor_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = ProductionPresetStore(Path(temp) / "production-presets.json")
            first = store.save(
                {
                    "name": "团队方案",
                    "recipe": {"production_settings": {"output_fps": 60}},
                },
                updated_by="employee-1",
            )
            second = store.save(
                {
                    **first,
                    "description": "第二版",
                },
                updated_by="employee-2",
            )

            self.assertEqual(first["revision"], 1)
            self.assertEqual(second["revision"], 2)
            self.assertNotEqual(first["content_hash"], second["content_hash"])
            self.assertEqual(second["updated_by"], "employee-2")
            self.assertTrue(second["updated_at"])

    def test_employee_can_only_list_update_and_delete_their_own_personal_preset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = ProductionPresetStore(Path(temp) / "production-presets.json")
            mine = store.save(
                {
                    "name": "我的方案",
                    "recipe": {"production_settings": {"output_fps": 60}},
                },
                updated_by="employee-a",
                can_manage_all=False,
            )
            self.assertEqual(mine["owner_user_id"], "employee-a")
            self.assertEqual(mine["scope"], "personal")
            self.assertTrue(mine["editable"])
            employee_b = store.list(
                viewer_user_id="employee-b", can_manage_all=False
            )
            self.assertNotIn(mine["id"], {item["id"] for item in employee_b})
            with self.assertRaisesRegex(PermissionError, "自己创建"):
                store.save(
                    {**mine, "description": "forged"},
                    updated_by="employee-b",
                    can_manage_all=False,
                )
            with self.assertRaisesRegex(PermissionError, "自己创建"):
                store.delete(
                    mine["id"],
                    updated_by="employee-b",
                    can_manage_all=False,
                )
            updated = store.save(
                {**mine, "description": "mine v2"},
                updated_by="employee-a",
                can_manage_all=False,
            )
            self.assertEqual(updated["revision"], 2)
            deleted = store.delete(
                mine["id"],
                updated_by="employee-a",
                can_manage_all=False,
            )
            self.assertTrue(deleted["deleted"])

    def test_legacy_ownerless_team_preset_is_hidden_from_every_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "production-presets.json"
            path.write_text(
                """{
  "schema_version": 2,
  "presets": [{
    "id": "legacy_team",
    "name": "Legacy team",
    "description": "",
    "recipe": {"production_settings": {"output_fps": 60}},
    "updated_by": "old-editor"
  }]
}""",
                encoding="utf-8",
            )
            store = ProductionPresetStore(path)
            employee_listing = store.list(
                viewer_user_id="employee-a", can_manage_all=False
            )
            admin_listing = store.list(can_manage_all=True)
            self.assertEqual(employee_listing, [])
            self.assertEqual(admin_listing, [])
            self.assertIn("legacy_team", store._read_overrides())

    def test_recipe_rejects_credentials_and_commands(self) -> None:
        for key in ("api_key", "password", "providers", "command", "ffmpeg_path"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "cannot store"):
                validate_production_preset(
                    {"name": "unsafe", "recipe": {key: "secret"}}
                )

    def test_recipe_rejects_content_ids_local_paths_and_unknown_nested_styles(self) -> None:
        for value in (
            {"platform_id": "goodnovel"},
            {"production_settings": {"video_folder": r"D:\\media"}},
            {"production_settings": {"subtitle": {"novel_id": "n-1"}}},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_production_preset({"name": "unsafe", "recipe": value})

    def test_recipe_rejects_out_of_range_values_before_they_reach_rendering(self) -> None:
        invalid_settings = (
            {"narration_wpm": 9999},
            {"bgm_volume": 2},
            {"end_card_seconds": 30},
            {"output_fps": 120},
            {"subtitle": {"font_size": 999999}},
        )
        for settings in invalid_settings:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                validate_production_preset(
                    {
                        "name": "invalid range",
                        "recipe": {"production_settings": settings},
                    }
                )

    def test_recipe_preserves_strict_narration_audio_export_choice(self) -> None:
        parsed = validate_production_preset(
            {
                "name": "audio sidecar",
                "recipe": {
                    "production_settings": {
                        "export_narration_audio": True,
                    }
                },
            }
        )
        self.assertTrue(
            parsed["recipe"]["production_settings"]["export_narration_audio"]
        )
        for invalid in (1, "true", None):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "must be a boolean"
            ):
                validate_production_preset(
                    {
                        "name": "invalid audio sidecar",
                        "recipe": {
                            "production_settings": {
                                "export_narration_audio": invalid,
                            }
                        },
                    }
                )

    def test_recipe_preserves_only_supported_output_contracts(self) -> None:
        for mode in ("video_and_mp3", "audio_only", "reuse_audio"):
            with self.subTest(mode=mode):
                parsed = validate_production_preset(
                    {
                        "name": "output contract",
                        "recipe": {"production_settings": {"output_mode": mode}},
                    }
                )
                self.assertEqual(
                    parsed["recipe"]["production_settings"]["output_mode"],
                    mode,
                )
        with self.assertRaisesRegex(ValueError, "invalid production preset option"):
            validate_production_preset(
                {
                    "name": "video only is forbidden",
                    "recipe": {"production_settings": {"output_mode": "video_only"}},
                }
            )

    def test_recipe_persists_video_subtitle_and_bgm_controls(self) -> None:
        parsed = validate_production_preset(
            {
                "name": "custom controls",
                "recipe": {
                    "production_settings": {
                        "narration_wpm": 280,
                        "video_playback_speed": 0.8,
                        "video_transition": "fade",
                        "subtitle_word_mode": "single",
                        "bgm_mode": "manual",
                        "bgm_file": r"D:\music\theme.mp3",
                    }
                },
            }
        )
        settings = parsed["recipe"]["production_settings"]
        self.assertEqual(settings["narration_wpm"], 280)
        self.assertEqual(settings["video_playback_speed"], 0.8)
        self.assertEqual(settings["video_transition"], "fade")
        self.assertEqual(settings["subtitle_word_mode"], "single")
        self.assertEqual(settings["bgm_mode"], "manual")
        self.assertEqual(settings["bgm_file"], r"D:\music\theme.mp3")
        with self.assertRaisesRegex(ValueError, "requires bgm_file"):
            validate_production_preset(
                {
                    "name": "manual music without file",
                    "recipe": {
                        "production_settings": {"bgm_mode": "manual"}
                    },
                }
            )

    def test_recipe_allows_only_a_boolean_cover_outro_choice(self) -> None:
        parsed = validate_production_preset(
            {
                "name": "caption-only ending",
                "recipe": {
                    "production_settings": {
                        "cover_outro_enabled": False,
                    }
                },
            }
        )
        self.assertFalse(
            parsed["recipe"]["production_settings"]["cover_outro_enabled"]
        )
        for invalid in (0, "false", None):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "must be a boolean"
            ):
                validate_production_preset(
                    {
                        "name": "invalid ending switch",
                        "recipe": {
                            "production_settings": {
                                "cover_outro_enabled": invalid,
                            }
                        },
                    }
                )

    def test_catalog_scopes_team_and_personal_presets_by_authenticated_actor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "storyforge-catalog.sqlite3"
            first = CatalogRepository(database)
            first.save_user({"username": "preset-admin", "role": "admin"})
            employee = first.save_user(
                {"username": "preset-owner", "role": "producer"}
            )
            other = first.save_user(
                {"username": "preset-other", "role": "producer"}
            )
            saved = first.save_production_preset(
                {
                    "name": "团队快速方案",
                    "recipe": {
                        "story_mood": "revenge",
                        "target_video_count": 325,
                        "production_settings": {"output_fps": 60},
                    },
                },
                actor_user_id=employee["id"],
            )

            restarted = CatalogRepository(database)
            listing = restarted.list_production_presets(
                actor_user_id=employee["id"]
            )
            selected = next(
                item for item in listing["items"] if item["id"] == saved["id"]
            )
            self.assertEqual(selected["recipe"]["target_video_count"], 325)
            self.assertEqual(listing["total"], 1)
            other_listing = restarted.list_production_presets(
                actor_user_id=other["id"]
            )
            self.assertNotIn(
                saved["id"], {item["id"] for item in other_listing["items"]}
            )


if __name__ == "__main__":
    unittest.main()
