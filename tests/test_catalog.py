from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from storyforge.catalog import (
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogRepository,
    CatalogValidationError,
    DuplicateContentError,
    KNOWN_PERMISSIONS,
    PromoCodeLimitError,
    SCHEMA_VERSION,
    installation_id_sha256,
    normalize_portable_device_config,
)


class CatalogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database_path = Path(self.temporary.name) / "catalog.sqlite3"
        self.catalog = CatalogRepository(
            self.database_path,
            site_id="test-site",
            site_name="Test Studio",
            busy_timeout_ms=3000,
        )

    def import_story(
        self,
        *,
        title: str = "A Quiet Door",
        body: str = "Chapter one begins.\n\nA secret appears.",
        with_episodes: bool = True,
    ) -> dict:
        payload: dict = {
            "title": title,
            "body": body,
            "synopsis": "A compact test story.",
            "language": "en-US",
            "chapters": [
                {"ordinal": 1, "title": "Chapter 1", "body": body},
            ],
        }
        if with_episodes:
            payload["episodes"] = [
                {
                    "ordinal": 1,
                    "title": "Opening",
                    "source_map": [{"chapter": 1, "start": 0, "end": 2}],
                    "estimated_duration_seconds": 42.5,
                }
            ]
        return self.catalog.import_novel(payload)

    def bind_story(self, novel_id: str, platform_name: str = "GoodNovel") -> dict:
        return self.catalog.save_novel_binding(
            {
                "novel_id": novel_id,
                "platform_name": platform_name,
                "external_book_id": f"book-{novel_id[:6]}",
                "platform_title": "Platform Title",
                "commission_rate": 70,
            }
        )

    def add_code(self, binding_id: str, code: str = "B73165") -> dict:
        return self.catalog.add_promo_code(
            {"binding_id": binding_id, "code": code}
        )

    def test_hub_user_tokens_are_hashed_resolvable_and_revocable(self) -> None:
        self.catalog.save_user(
            {"username": "owner", "role": "admin", "active": True}
        )
        user = self.catalog.save_user(
            {"username": "render-pc-1", "role": "producer", "active": True}
        )
        issued = self.catalog.issue_hub_access_token(
            user["id"], label="Studio PC 1"
        )

        self.assertTrue(issued["token"].startswith("sfh_"))
        self.assertEqual(
            self.catalog.resolve_hub_access_token(issued["token"]), user["id"]
        )
        listed = self.catalog.list_hub_access_tokens(user["id"])
        self.assertEqual(listed["total"], 1)
        self.assertNotIn("token", listed["items"][0])
        connection = sqlite3.connect(self.database_path)
        try:
            stored = connection.execute(
                "SELECT token_hash FROM hub_access_tokens WHERE id = ?",
                (issued["id"],),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotEqual(stored, issued["token"])
        self.assertNotIn(issued["token"], self.database_path.read_bytes().decode("latin1"))

        revoked = self.catalog.revoke_hub_access_token(issued["id"])
        self.assertTrue(revoked["revoked"])
        self.assertIsNone(self.catalog.resolve_hub_access_token(issued["token"]))

    def test_cannot_issue_hub_token_for_inactive_user(self) -> None:
        self.catalog.save_user(
            {"username": "owner", "role": "admin", "active": True}
        )
        user = self.catalog.save_user(
            {"username": "disabled-renderer", "role": "producer", "active": False}
        )
        with self.assertRaisesRegex(CatalogValidationError, "inactive"):
            self.catalog.issue_hub_access_token(user["id"], label="Render PC")

    def test_hub_token_requires_a_device_label(self) -> None:
        self.catalog.save_user(
            {"username": "owner", "role": "admin", "active": True}
        )
        user = self.catalog.save_user(
            {"username": "render-pc", "role": "producer", "active": True}
        )
        with self.assertRaisesRegex(CatalogValidationError, "label"):
            self.catalog.issue_hub_access_token(user["id"], label="   ")

    def make_draft(
        self,
        *,
        novel: dict | None = None,
        binding: dict | None = None,
        code: dict | None = None,
        publishing_account_id: str | None = None,
        creative_line_count: int = 3,
    ) -> tuple[dict, dict, dict, dict]:
        novel = novel or self.import_story()["novel"]
        binding = binding or self.bind_story(novel["id"])
        code = code or self.add_code(binding["id"])
        episode_id = novel["current_revision"]["episodes"][0]["id"]
        draft = self.catalog.save_draft(
            {
                "novel_id": novel["id"],
                "binding_id": binding["id"],
                "promo_code_id": code["id"],
                "publishing_account_id": publishing_account_id,
                "creative_line_count": creative_line_count,
                "episode_ids": [episode_id],
                "voice_profile": "af_heart",
            }
        )
        return novel, binding, code, draft


class DeviceManagementTests(CatalogTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.catalog.save_user(
            {"username": "device-owner", "role": "admin", "active": True}
        )
        self.worker = self.catalog.save_user(
            {"username": "worker", "role": "producer", "active": True}
        )

    def register_device(self, installation: str, name: str = "Editing PC") -> dict:
        return self.catalog.register_hub_device(
            {
                "installation_id_hash": installation_id_sha256(installation),
                "name": name,
                "hostname": name.replace(" ", "-"),
                "app_version": "0.3.3",
                "os_name": "Windows",
                "architecture": "x64",
                "capabilities": {"local_tts": True, "local_render": True},
            },
            actor_user_id=self.worker["id"],
        )["device"]

    def test_portable_config_accepts_and_normalizes_retired_subtitle_presets(self) -> None:
        word = normalize_portable_device_config(
            {
                "subtitle_preset": "word_pop_sync",
                "subtitle_word_mode": "off",
                "subtitle": {"active_color": "#12abef"},
            }
        )
        minimal = normalize_portable_device_config(
            {
                "subtitle_preset": "minimal_bottom",
                "subtitle": {"bottom_margin": 275},
            }
        )

        self.assertEqual(word["subtitle_preset"], "clear_outline")
        self.assertEqual(word["subtitle_word_mode"], "single")
        self.assertEqual(word["subtitle"]["active_color"], "#12ABEF")
        self.assertEqual(minimal["subtitle_preset"], "clear_outline")
        self.assertEqual(minimal["subtitle"]["bottom_margin"], 275)

    def test_registration_reuses_installation_not_name_and_device_can_be_managed(self) -> None:
        first = self.register_device("installation-one")
        reconnected = self.catalog.register_hub_device(
            {
                "installation_id_hash": installation_id_sha256("installation-one"),
                "name": "Renamed at Login",
                "hostname": "edit-01",
                "app_version": "0.3.4",
            },
            actor_user_id=self.worker["id"],
        )
        same_name_other_install = self.register_device(
            "installation-two", name="Renamed at Login"
        )

        self.assertTrue(reconnected["reused"])
        self.assertEqual(reconnected["device"]["id"], first["id"])
        self.assertNotEqual(same_name_other_install["id"], first["id"])
        self.assertEqual(self.catalog.list_hub_devices()["total"], 2)
        self.assertEqual(self.catalog.hub_device_fleet_summary()["online"], 2)

        renamed = self.catalog.rename_hub_device(
            first["id"], "Production PC 01", actor_user_id=self.owner["id"]
        )
        self.assertEqual(renamed["name"], "Production PC 01")
        issued = self.catalog.issue_hub_access_token(
            self.worker["id"],
            label="Production PC 01",
            device_id=first["id"],
            actor_user_id=self.owner["id"],
        )
        identity = self.catalog.resolve_hub_access_identity(issued["token"])
        self.assertTrue(identity["authenticated"])
        self.assertEqual(identity["device_id"], first["id"])
        rotated = self.catalog.rotate_hub_device_access_token(
            self.worker["id"],
            first["id"],
            label="Production PC 01",
            actor_user_id=self.worker["id"],
        )
        self.assertEqual(rotated["replaced_token_count"], 1)
        self.assertIsNone(self.catalog.resolve_hub_access_token(issued["token"]))
        self.assertEqual(
            self.catalog.resolve_hub_access_token(rotated["token"]), self.worker["id"]
        )

        disabled = self.catalog.set_hub_device_active(
            first["id"], False, actor_user_id=self.owner["id"]
        )
        self.assertFalse(disabled["device"]["active"])
        self.assertEqual(disabled["revoked_tokens"], 1)
        self.assertIsNone(self.catalog.resolve_hub_access_token(rotated["token"]))
        with self.assertRaisesRegex(CatalogConflictError, "inactive"):
            self.catalog.heartbeat_hub_device(first["id"])

        actions = {
            item["action"] for item in self.catalog.list_audit_events(limit=500)["items"]
        }
        self.assertTrue(
            {
                "hub_device.registered",
                "hub_device.renamed",
                "hub_device.activation_changed",
                "hub_token.issued",
                "hub_token.rotated",
            }
            <= actions
        )

    def test_offline_ttl_and_paged_status_filters(self) -> None:
        old_device = self.register_device("old-install", "Old PC")
        current_device = self.register_device("new-install", "Current PC")
        with self.catalog._write_connection() as connection:
            connection.execute(
                "UPDATE hub_devices SET last_seen_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00+00:00", old_device["id"]),
            )

        offline = self.catalog.list_hub_devices(
            online=False, offline_after_seconds=60, limit=1
        )
        online = self.catalog.list_hub_devices(
            online=True, offline_after_seconds=60
        )
        self.assertEqual(offline["total"], 1)
        self.assertEqual(offline["items"][0]["id"], old_device["id"])
        self.assertEqual(online["total"], 1)
        self.assertEqual(online["items"][0]["id"], current_device["id"])
        heartbeat = self.catalog.heartbeat_hub_device(
            old_device["id"], app_version="0.3.5"
        )
        self.assertTrue(heartbeat["device"]["online"])
        self.assertEqual(heartbeat["device"]["app_version"], "0.3.5")

    def test_disabled_obsolete_device_can_be_deleted_without_record_clutter(self) -> None:
        device = self.register_device("obsolete-install", "Old Editing PC")
        issued = self.catalog.issue_hub_access_token(
            self.worker["id"],
            label="Old Editing PC",
            device_id=device["id"],
            actor_user_id=self.owner["id"],
        )
        with self.assertRaisesRegex(CatalogConflictError, "disabled"):
            self.catalog.delete_hub_device(
                device["id"], actor_user_id=self.owner["id"]
            )

        self.catalog.set_hub_device_active(
            device["id"], False, actor_user_id=self.owner["id"]
        )
        deleted = self.catalog.delete_hub_device(
            device["id"], actor_user_id=self.owner["id"]
        )

        self.assertTrue(deleted["deleted"])
        self.assertEqual(self.catalog.list_hub_devices()["total"], 0)
        self.assertIsNone(self.catalog.resolve_hub_access_token(issued["token"]))
        with self.assertRaises(CatalogNotFoundError):
            self.catalog.get_hub_device(device["id"])
        actions = {
            item["action"] for item in self.catalog.list_audit_events(limit=500)["items"]
        }
        self.assertIn("hub_device.deleted", actions)

    def test_single_multiple_all_targets_and_ack_are_device_scoped(self) -> None:
        first = self.register_device("target-1", "PC 1")
        second = self.register_device("target-2", "PC 2")
        third = self.register_device("target-3", "PC 3")
        single = self.catalog.create_device_config_revision(
            {
                "config": {
                    "output_fps": 60,
                    "bgm_volume": 0.35,
                    "subtitle_preset": "word_pop_sync",
                    "subtitle": {
                        "font_size": 52,
                        "position_x_percent": 50,
                        "active_color": "#ffe06a",
                    },
                },
                "target_mode": "single",
                "device_ids": [first["id"]],
                "note": "60 FPS captions",
            },
            actor_user_id=self.owner["id"],
        )
        self.assertEqual(single["target_count"], 1)
        self.assertTrue(
            self.catalog.get_device_desired_config(first["id"])["needs_apply"]
        )
        self.assertIsNone(
            self.catalog.get_device_desired_config(second["id"])["desired"]
        )
        with self.assertRaisesRegex(LookupError, "target"):
            self.catalog.ack_device_config(second["id"], single["id"])
        with self.assertRaisesRegex(CatalogConflictError, "hash"):
            self.catalog.ack_device_config(
                first["id"], single["id"], reported_config_hash="0" * 64
            )
        acknowledged = self.catalog.ack_device_config(
            first["id"],
            single["id"],
            reported_config_hash=single["config_hash"],
            actor_user_id=self.worker["id"],
        )
        self.assertEqual(acknowledged["status"], "applied")
        revision_detail = self.catalog.get_device_config_revision(single["id"])
        self.assertEqual(revision_detail["targets"][0]["ack_status"], "applied")
        desired = self.catalog.get_device_desired_config(
            first["id"], current_revision_id=single["id"]
        )
        self.assertFalse(desired["needs_apply"])

        multiple = self.catalog.create_device_config_revision(
            {
                "config": {"preview_seconds": 8},
                "target_mode": "multiple",
                "device_ids": [first["id"], second["id"]],
            },
            actor_user_id=self.owner["id"],
        )
        self.assertEqual(
            set(multiple["target_device_ids"]), {first["id"], second["id"]}
        )
        self.catalog.set_hub_device_active(
            third["id"], False, actor_user_id=self.owner["id"]
        )
        everyone_active = self.catalog.create_device_config_revision(
            {
                "config": {"caption_mode": "semantic"},
                "target_mode": "all",
            },
            actor_user_id=self.owner["id"],
        )
        self.assertEqual(
            set(everyone_active["target_device_ids"]), {first["id"], second["id"]}
        )
        self.assertEqual(self.catalog.list_device_config_revisions()["total"], 3)

    def test_portable_config_rejects_machine_local_secrets_and_bad_ranges(self) -> None:
        device = self.register_device("safe-target")
        invalid_configs = (
            {"video_encoder": "h264_nvenc"},
            {"providers": {"text_endpoint": "https://example.test"}},
            {"api_key": "secret"},
            {"output_fps": 55},
            {"bgm_volume": 1.2},
            {"narration_wpm": 199},
            {"narration_wpm": 281},
            {"video_playback_speed": 3.1},
            {"retention_min": 0.95, "retention_max": 0.8},
            {"subtitle": {"font_family": r"C:\Windows\Fonts\Arial.ttf"}},
            {"subtitle": {"unexpected": 1}},
        )
        for config in invalid_configs:
            with self.subTest(config=config), self.assertRaises(CatalogValidationError):
                self.catalog.create_device_config_revision(
                    {
                        "config": config,
                        "target_mode": "single",
                        "device_ids": [device["id"]],
                    },
                    actor_user_id=self.owner["id"],
                )

    def test_portable_config_accepts_new_safe_production_controls(self) -> None:
        device = self.register_device("new-controls")
        saved = self.catalog.create_device_config_revision(
            {
                "config": {
                    "output_mode": "audio_only",
                    "narration_wpm": 280,
                    "video_playback_speed": 0.8,
                    "video_transition": "fade",
                    "subtitle_word_mode": "single",
                    "bgm_mode": "none",
                },
                "target_mode": "single",
                "device_ids": [device["id"]],
            },
            actor_user_id=self.owner["id"],
        )

        self.assertEqual(saved["config"]["narration_wpm"], 280)
        self.assertEqual(saved["config"]["video_playback_speed"], 0.8)
        self.assertEqual(saved["config"]["video_transition"], "fade")
        self.assertEqual(saved["config"]["subtitle_word_mode"], "single")
        self.assertEqual(saved["config"]["bgm_mode"], "none")

    def test_cross_site_or_inactive_targets_cannot_be_forged(self) -> None:
        local = self.register_device("local-target")
        other_catalog = CatalogRepository(
            self.database_path, site_id="other-site", site_name="Other Studio"
        )
        foreign = other_catalog.register_hub_device(
            {
                "installation_id_hash": installation_id_sha256("foreign-target"),
                "name": "Foreign PC",
            }
        )["device"]
        for target_id in (foreign["id"], "forged-device-id"):
            with self.subTest(target_id=target_id), self.assertRaisesRegex(
                CatalogValidationError, "belong"
            ):
                self.catalog.create_device_config_revision(
                    {
                        "config": {"output_fps": 60},
                        "target_mode": "single",
                        "device_ids": [target_id],
                    },
                    actor_user_id=self.owner["id"],
                )
        self.catalog.set_hub_device_active(local["id"], False)
        with self.assertRaisesRegex(CatalogValidationError, "active"):
            self.catalog.create_device_config_revision(
                {
                    "config": {"output_fps": 60},
                    "target_mode": "single",
                    "device_ids": [local["id"]],
                }
            )

    def test_version_ten_migration_preserves_legacy_tokens(self) -> None:
        issued = self.catalog.issue_hub_access_token(
            self.worker["id"], label="Legacy unbound PC"
        )
        with self.catalog._write_connection() as connection:
            connection.execute("DROP INDEX IF EXISTS idx_hub_tokens_device")
            connection.execute("DROP TABLE device_config_targets")
            connection.execute("DROP TABLE device_config_revisions")
            connection.execute("ALTER TABLE hub_access_tokens DROP COLUMN device_id")
            connection.execute("DROP TABLE hub_devices")
            connection.execute("DELETE FROM schema_migrations WHERE version = 10")

        migrated = CatalogRepository(
            self.database_path, site_id="test-site", site_name="Test Studio"
        )
        self.assertEqual(migrated.bootstrap_summary()["schema_version"], SCHEMA_VERSION)
        self.assertEqual(
            migrated.resolve_hub_access_token(issued["token"]), self.worker["id"]
        )
        self.assertEqual(
            migrated.list_hub_access_tokens(self.worker["id"])["items"][0][
                "device_id"
            ],
            "",
        )
        with migrated._read_connection() as connection:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            token_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(hub_access_tokens)")
            }
        self.assertTrue(
            {"hub_devices", "device_config_revisions", "device_config_targets"}
            <= tables
        )
        self.assertIn("device_id", token_columns)


class SchemaAndNovelTests(CatalogTestCase):
    def test_connection_identity_is_bounded_and_does_not_open_sqlite(self) -> None:
        with patch.object(
            self.catalog,
            "_read_connection",
            side_effect=AssertionError("health identity must not open SQLite"),
        ):
            identity = self.catalog.connection_identity()

        self.assertEqual(
            identity,
            {
                "schema_version": SCHEMA_VERSION,
                "site": {"id": "test-site", "name": "Test Studio"},
            },
        )
        self.assertNotIn("counts", identity)
        self.assertNotIn("journal_mode", identity)

    def test_schema_uses_wal_foreign_keys_busy_timeout_and_summary(self) -> None:
        summary = self.catalog.bootstrap_summary()

        self.assertEqual(summary["schema_version"], SCHEMA_VERSION)
        self.assertEqual(summary["journal_mode"], "wal")
        self.assertEqual(summary["site"]["id"], "test-site")
        self.assertEqual(summary["counts"]["novels"], 0)
        with self.catalog._read_connection() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 3000)

    def test_memory_database_is_rejected_because_connections_are_per_operation(self) -> None:
        with self.assertRaisesRegex(CatalogValidationError, "file-backed"):
            CatalogRepository(":memory:")

    def test_reopening_catalog_is_idempotent_and_preserves_rows(self) -> None:
        novel = self.import_story()["novel"]
        reopened = CatalogRepository(
            self.database_path, site_id="test-site", site_name="Test Studio"
        )

        self.assertEqual(reopened.list_novels()["total"], 1)
        self.assertEqual(reopened.get_novel(novel["id"])["title"], novel["title"])
        with reopened._read_connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
                SCHEMA_VERSION,
            )

    def test_version_eight_adds_safe_platform_branding_defaults(self) -> None:
        self.catalog.save_platform({"id": "legacy", "name": "Legacy Platform"})
        with self.catalog._write_connection() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version = 8")
            connection.execute("ALTER TABLE platforms DROP COLUMN brand_color")
            connection.execute("ALTER TABLE platforms DROP COLUMN logo_path")

        migrated = CatalogRepository(
            self.database_path, site_id="test-site", site_name="Test Studio"
        )

        with migrated._read_connection() as connection:
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(platforms)")
            }
        self.assertIn("logo_path", columns)
        self.assertIn("brand_color", columns)
        platform = migrated.list_platforms()["items"][0]
        self.assertEqual(platform["logo_path"], "")
        self.assertEqual(platform["brand_color"], "")

    def test_version_five_replans_unreferenced_current_revision_once(self) -> None:
        body = """Chapter 1: The call
Mara answered the phone and heard a familiar voice.

Chapter 2: The photograph
She opened the drawer and found the missing photograph.

Chapter 3: The visitor
At midnight, someone knocked and called her by name."""
        imported = self.catalog.import_novel(
            {
                "title": "Three authored chapters",
                "body": body,
                "revision_metadata": {"legacy_note": "preserve me"},
                "chapters": [
                    {"ordinal": 1, "title": "Legacy block", "body": body}
                ],
                "episodes": [
                    {
                        "ordinal": 1,
                        "title": "Legacy merged episode",
                        "source_map": [
                            {"chapter_ordinals": [1, 2, 3], "start_word": 0, "end_word": 35}
                        ],
                        "metadata": {"legacy": True},
                    }
                ],
            }
        )["novel"]
        revision_id = imported["current_revision"]["id"]
        old_episode_id = imported["current_revision"]["episodes"][0]["id"]

        with self.catalog._write_connection() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version >= 6")

        migrated = CatalogRepository(
            self.database_path,
            site_id="test-site",
            site_name="Test Studio",
        )
        revision = migrated.get_novel(imported["id"])["current_revision"]
        self.assertEqual(revision["id"], revision_id)
        self.assertEqual(
            [chapter["title"] for chapter in revision["chapters"]],
            [
                "Chapter 1: The call",
                "Chapter 2: The photograph",
                "Chapter 3: The visitor",
            ],
        )
        self.assertEqual(
            [episode["title"] for episode in revision["episodes"]],
            [
                "Chapter 1: The call",
                "Chapter 2: The photograph",
                "Chapter 3: The visitor",
            ],
        )
        self.assertNotEqual(revision["episodes"][0]["id"], old_episode_id)
        self.assertEqual(revision["metadata"]["planner_version"], 4)
        self.assertEqual(revision["metadata"]["estimator_version"], 2)
        self.assertEqual(revision["metadata"]["legacy_note"], "preserve me")

        episode_ids = [episode["id"] for episode in revision["episodes"]]
        with migrated._write_connection() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version >= 6")
        reopened = CatalogRepository(
            self.database_path,
            site_id="test-site",
            site_name="Test Studio",
        )
        repeated = reopened.get_novel(imported["id"])["current_revision"]
        self.assertEqual(
            [episode["id"] for episode in repeated["episodes"]], episode_ids
        )
        self.assertEqual(repeated["metadata"]["planner_version"], 4)

    def test_version_five_preserves_revisions_referenced_by_draft_or_record(self) -> None:
        draft_novel = self.catalog.import_novel(
            {
                "title": "Draft referenced",
                "body": "Chapter 1\nA draft must keep this legacy episode.",
                "revision_metadata": {"legacy_plan": "draft"},
                "chapters": [
                    {
                        "ordinal": 1,
                        "title": "Legacy draft chapter",
                        "body": "Chapter 1\nA draft must keep this legacy episode.",
                    }
                ],
                "episodes": [
                    {
                        "ordinal": 1,
                        "title": "Frozen for draft",
                        "source_map": [{"chapter_ordinals": [1]}],
                        "metadata": {"legacy": "draft"},
                    }
                ],
            }
        )["novel"]
        draft_binding = self.bind_story(draft_novel["id"])
        draft_code = self.add_code(draft_binding["id"], "DRAFT-1")
        self.catalog.save_draft(
            {
                "novel_id": draft_novel["id"],
                "binding_id": draft_binding["id"],
                "promo_code_id": draft_code["id"],
                "creative_line_count": 1,
                "episode_ids": [
                    draft_novel["current_revision"]["episodes"][0]["id"]
                ],
            }
        )

        record_novel = self.catalog.import_novel(
            {
                "title": "Record referenced",
                "body": "Chapter 1\nA production record must keep this legacy episode.",
                "revision_metadata": {"legacy_plan": "record"},
                "chapters": [
                    {
                        "ordinal": 1,
                        "title": "Legacy record chapter",
                        "body": "Chapter 1\nA production record must keep this legacy episode.",
                    }
                ],
                "episodes": [
                    {
                        "ordinal": 1,
                        "title": "Frozen for record",
                        "source_map": [{"chapter_ordinals": [1]}],
                        "metadata": {"legacy": "record"},
                    }
                ],
            }
        )["novel"]
        record_binding = self.bind_story(record_novel["id"])
        record_code = self.add_code(record_binding["id"], "RECORD-1")
        record_draft = self.catalog.save_draft(
            {
                "novel_id": record_novel["id"],
                "binding_id": record_binding["id"],
                "promo_code_id": record_code["id"],
                "creative_line_count": 1,
                "episode_ids": [
                    record_novel["current_revision"]["episodes"][0]["id"]
                ],
            }
        )
        self.catalog.save_production_record(
            {
                "draft_id": record_draft["id"],
                "job_id": "planner-v2-reference",
                "episode_id": record_novel["current_revision"]["episodes"][0]["id"],
            }
        )
        # Leave only the direct production-record reference for this novel.
        with self.catalog._write_connection() as connection:
            connection.execute(
                "DELETE FROM draft_episodes WHERE draft_id = ?", (record_draft["id"],)
            )

        before_draft = self.catalog.get_novel(draft_novel["id"])["current_revision"]
        before_record = self.catalog.get_novel(record_novel["id"])["current_revision"]
        with self.catalog._write_connection() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version >= 6")

        migrated = CatalogRepository(
            self.database_path,
            site_id="test-site",
            site_name="Test Studio",
        )
        after_draft = migrated.get_novel(draft_novel["id"])["current_revision"]
        after_record = migrated.get_novel(record_novel["id"])["current_revision"]
        self.assertEqual(
            [item["id"] for item in after_draft["episodes"]],
            [item["id"] for item in before_draft["episodes"]],
        )
        self.assertEqual(
            [item["id"] for item in after_record["episodes"]],
            [item["id"] for item in before_record["episodes"]],
        )
        self.assertEqual(after_draft["metadata"]["planner_version"], 4)
        self.assertEqual(after_record["metadata"]["planner_version"], 4)
        self.assertEqual(after_draft["metadata"]["estimator_version"], 2)
        self.assertEqual(after_record["metadata"]["estimator_version"], 2)

    def test_version_twelve_separates_authored_chapters_and_remaps_draft(self) -> None:
        body = """Chapter 1
The first chapter is intentionally short.

Chapter 2
The second chapter is also intentionally short.

Chapter 3
The third chapter ends the imported excerpt."""
        novel = self.catalog.import_novel(
            {
                "title": "Legacy merged authored chapters",
                "body": body,
                "revision_metadata": {
                    "planner_version": 3,
                    "estimator_version": 2,
                    "estimate_wpm": 225,
                },
                "chapters": [
                    {"ordinal": 1, "title": "Chapter 1", "body": "The first chapter is intentionally short."},
                    {"ordinal": 2, "title": "Chapter 2", "body": "The second chapter is also intentionally short."},
                    {"ordinal": 3, "title": "Chapter 3", "body": "The third chapter ends the imported excerpt."},
                ],
                "episodes": [
                    {
                        "ordinal": 1,
                        "title": "Chapter 1 / Chapter 2",
                        "source_map": [{"chapter_ordinals": [1, 2]}],
                        "recap_text": "A manually written merged recap.",
                        "status": "reviewed",
                        "metadata": {
                            "text": "legacy merged text",
                            "editor_note": "preserve in repair backup",
                        },
                    },
                    {
                        "ordinal": 2,
                        "title": "Chapter 3",
                        "source_map": [{"chapter_ordinals": [3]}],
                        "recap_text": "A manually written third recap.",
                        "status": "approved",
                        "metadata": {
                            "text": "legacy final text",
                            "editor_note": "preserve on exact match",
                        },
                    },
                ],
            }
        )["novel"]
        binding = self.bind_story(novel["id"])
        code = self.add_code(binding["id"], "SPLIT-12")
        draft = self.catalog.save_draft(
            {
                "novel_id": novel["id"],
                "binding_id": binding["id"],
                "promo_code_id": code["id"],
                "creative_line_count": 1,
                "episode_ids": [
                    novel["current_revision"]["episodes"][0]["id"]
                ],
            }
        )
        old_episode_ids = [
            item["id"] for item in novel["current_revision"]["episodes"]
        ]
        with self.catalog._write_connection() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version = 12")

        migrated = CatalogRepository(
            self.database_path,
            site_id="test-site",
            site_name="Test Studio",
        )
        revision = migrated.get_novel(novel["id"])["current_revision"]
        self.assertEqual(
            [item["title"] for item in revision["episodes"]],
            ["Chapter 1", "Chapter 2", "Chapter 3"],
        )
        self.assertTrue(
            all(item["id"] not in old_episode_ids for item in revision["episodes"])
        )
        self.assertEqual(revision["metadata"]["planner_version"], 4)
        self.assertEqual(
            revision["episodes"][2]["recap_text"],
            "A manually written third recap.",
        )
        self.assertEqual(revision["episodes"][2]["status"], "approved")
        self.assertEqual(
            revision["episodes"][2]["metadata"]["editor_note"],
            "preserve on exact match",
        )
        backup = revision["metadata"]["episode_choice_repair_backup"]
        self.assertEqual(backup["reason"], "merged_authored_chapters")
        self.assertEqual(
            backup["episodes"][0]["metadata"]["editor_note"],
            "preserve in repair backup",
        )
        # The old selected episode covered chapters 1 and 2.  The migration
        # preserves that intent as two independently selectable choices.
        self.assertEqual(
            migrated.get_draft(draft["id"])["episode_ids"],
            [revision["episodes"][0]["id"], revision["episodes"][1]["id"]],
        )

        episode_ids = [item["id"] for item in revision["episodes"]]
        with migrated._write_connection() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version = 12")
        reopened = CatalogRepository(
            self.database_path,
            site_id="test-site",
            site_name="Test Studio",
        )
        self.assertEqual(
            [
                item["id"]
                for item in reopened.get_novel(novel["id"])["current_revision"][
                    "episodes"
                ]
            ],
            episode_ids,
        )

    def test_version_twelve_tolerates_malformed_source_map_without_losing_draft_choices(self) -> None:
        body = """Chapter 1
One.

Chapter 2
Two.

Chapter 3
Three."""
        novel = self.catalog.import_novel(
            {
                "title": "Malformed legacy episode map",
                "body": body,
                "revision_metadata": {"planner_version": 3},
                "episodes": [
                    {
                        "ordinal": 1,
                        "title": "Chapter 1 / Chapter 2",
                        "source_map": [{"chapter_ordinals": [1, 2]}],
                    },
                    {
                        "ordinal": 2,
                        "title": "Damaged Chapter 3",
                        "source_map": [{"chapter_ordinals": 3}],
                    },
                ],
            }
        )["novel"]
        binding = self.bind_story(novel["id"])
        code = self.add_code(binding["id"], "DAMAGED-12")
        draft = self.catalog.save_draft(
            {
                "novel_id": novel["id"],
                "binding_id": binding["id"],
                "promo_code_id": code["id"],
                "creative_line_count": 1,
                "episode_ids": [
                    item["id"] for item in novel["current_revision"]["episodes"]
                ],
            }
        )
        with self.catalog._write_connection() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version = 12")

        migrated = CatalogRepository(
            self.database_path,
            site_id="test-site",
            site_name="Test Studio",
        )
        revision = migrated.get_novel(novel["id"])["current_revision"]
        self.assertEqual(
            [item["title"] for item in revision["episodes"]],
            ["Chapter 1", "Chapter 2", "Chapter 3"],
        )
        self.assertEqual(
            migrated.get_draft(draft["id"])["episode_ids"],
            [item["id"] for item in revision["episodes"]],
        )

    def test_version_seven_repairs_japanese_duration_without_changing_episode_id(self) -> None:
        japanese = "彼女はその秘密を知った。それでも、真実を確かめるために走り続けた。" * 25
        novel = self.catalog.import_novel(
            {
                "title": "日本語の物語",
                "body": japanese,
                "language": "ja",
                "revision_metadata": {"planner_version": 2},
                "chapters": [
                    {"ordinal": 1, "title": "第1話", "body": japanese, "metadata": {"word_count": 1}}
                ],
                "episodes": [
                    {
                        "ordinal": 1,
                        "title": "第1話",
                        "estimated_duration_seconds": 60.0 / 210.0,
                        "source_map": [{"chapter_ordinals": [1], "start_word": 0, "end_word": 1}],
                        "metadata": {"text": japanese, "word_count": 1},
                    }
                ],
            }
        )["novel"]
        before = novel["current_revision"]["episodes"][0]
        with self.catalog._write_connection() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version = 7")

        migrated = CatalogRepository(
            self.database_path, site_id="test-site", site_name="Test Studio"
        )
        revision = migrated.get_novel(novel["id"])["current_revision"]
        after = revision["episodes"][0]
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["metadata"]["text"], japanese)
        self.assertGreater(after["metadata"]["word_count"], 250)
        self.assertGreater(after["estimated_duration_seconds"], 70)
        self.assertEqual(revision["metadata"]["planner_version"], 3)

    def test_version_one_catalog_is_migrated_with_lease_columns(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("DROP INDEX IF EXISTS idx_records_lease_expiry")
            for column in ("heartbeat_at", "lease_expires_at", "lease_owner_device"):
                connection.execute(
                    f"ALTER TABLE production_records DROP COLUMN {column}"
                )
            connection.execute("DELETE FROM schema_migrations WHERE version = 2")
            connection.commit()
        finally:
            connection.close()

        migrated = CatalogRepository(
            self.database_path, site_id="test-site", site_name="Test Studio"
        )
        self.assertEqual(
            migrated.bootstrap_summary()["schema_version"], SCHEMA_VERSION
        )
        with migrated._read_connection() as connection:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(production_records)")
            }
            self.assertTrue(
                {"lease_owner_device", "lease_expires_at", "heartbeat_at"}
                <= columns
            )

    def test_version_three_supervisor_migrates_to_producer_without_permission_changes(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy-v3.sqlite3"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE sites (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE software_users (
                    id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
                    username TEXT NOT NULL,
                    normalized_username TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL CHECK (role IN ('admin', 'supervisor', 'producer')),
                    password_hash TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    row_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(site_id, normalized_username)
                );
                CREATE TABLE user_permissions (
                    user_id TEXT NOT NULL REFERENCES software_users(id) ON DELETE CASCADE,
                    permission TEXT NOT NULL,
                    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, permission)
                );
                CREATE TABLE hub_access_tokens (
                    id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES software_users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL DEFAULT '',
                    revoked_at TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations VALUES (1, '2026-01-01T00:00:00Z');
                INSERT INTO schema_migrations VALUES (2, '2026-01-01T00:00:01Z');
                INSERT INTO schema_migrations VALUES (3, '2026-01-01T00:00:02Z');
                INSERT INTO sites VALUES (
                    'legacy-site', 'Legacy Studio',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                );
                INSERT INTO software_users(
                    id, site_id, username, normalized_username, display_name,
                    role, created_at, updated_at
                ) VALUES (
                    'legacy-owner', 'legacy-site', 'owner', 'owner', 'Owner',
                    'admin', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                );
                INSERT INTO software_users(
                    id, site_id, username, normalized_username, display_name,
                    role, created_at, updated_at
                ) VALUES (
                    'legacy-lead', 'legacy-site', 'lead', 'lead', 'Team Lead',
                    'supervisor', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                );
                INSERT INTO user_permissions VALUES (
                    'legacy-lead', 'records.export', 0, '2026-01-01T00:00:00Z'
                );
                INSERT INTO user_permissions VALUES (
                    'legacy-lead', 'users.manage', 1, '2026-01-01T00:00:00Z'
                );
                INSERT INTO user_permissions VALUES (
                    'legacy-lead', 'library.view', 0, '2026-01-01T00:00:00Z'
                );
                INSERT INTO hub_access_tokens VALUES (
                    'legacy-token', 'legacy-site', 'legacy-lead',
                    'legacy-token-hash', 'Legacy PC', NULL,
                    '2026-01-01T00:00:00Z'
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

        migrated = CatalogRepository(
            legacy_path, site_id="legacy-site", site_name="Legacy Studio"
        )
        self.assertEqual(migrated.bootstrap_summary()["schema_version"], SCHEMA_VERSION)

        migrated_users = {
            item["id"]: item for item in migrated.list_users()["items"]
        }
        self.assertEqual(migrated_users["legacy-owner"]["role"], "admin")
        self.assertEqual(migrated_users["legacy-lead"]["role"], "producer")
        permissions = migrated.get_effective_permissions("legacy-lead")
        former_supervisor_defaults = {
            "library.view",
            "library.edit",
            "platforms.manage",
            "promo_codes.use",
            "promo_codes.manage",
            "publishing_accounts.manage",
            "drafts.create",
            "drafts.manage_all",
            "samples.approve_own",
            "samples.approve_all",
            "records.view_own",
            "records.view_all",
            "records.export",
            "jobs.retry_own",
            "jobs.retry_all",
        }
        expected_effective = {
            permission: permission in former_supervisor_defaults
            for permission in sorted(KNOWN_PERMISSIONS)
        }
        expected_effective["records.export"] = False
        expected_effective["users.manage"] = True
        expected_effective["library.view"] = False
        self.assertEqual(permissions["effective"], expected_effective)
        producer_defaults = {
            "library.view",
            "promo_codes.use",
            "drafts.create",
            "samples.approve_own",
            "records.view_own",
            "jobs.retry_own",
            "production.execute",
            "voice.preview",
            "text.assist",
            "presets.manage_own",
            "updates.manage_own",
        }
        expected_overrides = (
            former_supervisor_defaults.symmetric_difference(producer_defaults)
            | {"users.manage", "library.view"}
        )
        self.assertEqual(set(permissions["overrides"]), expected_overrides)
        self.assertFalse(permissions["overrides"]["records.export"])
        self.assertTrue(permissions["overrides"]["users.manage"])
        self.assertFalse(permissions["overrides"]["library.view"])
        self.assertEqual(
            migrated.list_hub_access_tokens("legacy-lead")["items"][0]["label"],
            "Legacy PC",
        )
        with self.assertRaisesRegex(CatalogValidationError, "admin or producer"):
            migrated.save_user(
                {"username": "another-lead", "role": "supervisor", "active": True}
            )
        with migrated._read_connection() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            table_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'software_users'"
                ).fetchone()[0]
            )
            self.assertNotIn("supervisor", table_sql)

    def test_version_eight_catalog_adds_job_archive_columns_before_index(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("DROP INDEX IF EXISTS idx_records_archived")
            for column in (
                "archive_snapshot_json",
                "archived_by_user_id",
                "archived_at",
                "archived",
            ):
                connection.execute(
                    f"ALTER TABLE production_records DROP COLUMN {column}"
                )
            connection.execute("DELETE FROM schema_migrations WHERE version = 9")
            connection.commit()
        finally:
            connection.close()

        migrated = CatalogRepository(
            self.database_path, site_id="test-site", site_name="Test Studio"
        )

        self.assertEqual(migrated.bootstrap_summary()["schema_version"], SCHEMA_VERSION)
        with migrated._read_connection() as connection:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(production_records)")
            }
            self.assertTrue(
                {
                    "archived",
                    "archived_at",
                    "archived_by_user_id",
                    "archive_snapshot_json",
                }
                <= columns
            )
            indexes = {
                row["name"]
                for row in connection.execute("PRAGMA index_list(production_records)")
            }
            self.assertIn("idx_records_archived", indexes)

    def test_import_normalizes_whitespace_and_deduplicates_exact_body(self) -> None:
        first = self.catalog.import_novel(
            {"title": "First title", "body": "One   line.\r\n\r\nSecond line.  "}
        )
        duplicate = self.catalog.import_novel(
            {"title": "Different filename", "body": "One line.\n\nSecond line."}
        )

        self.assertTrue(first["created"])
        self.assertFalse(first["deduplicated"])
        self.assertFalse(duplicate["created"])
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(duplicate["novel"]["id"], first["novel"]["id"])
        self.assertEqual(self.catalog.list_novels()["total"], 1)

    def test_new_revision_is_immutable_and_duplicate_cannot_attach_to_other_novel(self) -> None:
        first = self.import_story(title="First", body="First body.")
        second = self.import_story(title="Second", body="Second body.")

        revision = self.catalog.import_novel(
            {
                "novel_id": first["novel"]["id"],
                "title": "First renamed",
                "body": "First body, revised.",
            }
        )

        self.assertEqual(revision["revision_number"], 2)
        self.assertEqual(revision["novel"]["revision_count"], 2)
        self.assertEqual(
            revision["novel"]["current_revision"]["body"], "First body, revised."
        )
        with self.assertRaises(DuplicateContentError) as caught:
            self.catalog.import_novel(
                {
                    "novel_id": second["novel"]["id"],
                    "title": "Second",
                    "body": "First body, revised.",
                }
            )
        self.assertEqual(caught.exception.novel_id, first["novel"]["id"])

    def test_chapters_episodes_and_episode_update_are_returned_as_json_dicts(self) -> None:
        imported = self.catalog.import_novel(
            {
                "title": "Serial",
                "body": "Alpha.\n\nBeta.",
                "chapters": [
                    {"ordinal": 1, "title": "One", "body": "Alpha."},
                    {"ordinal": 2, "title": "Two", "body": "Beta."},
                ],
                "episodes": [
                    {
                        "ordinal": 1,
                        "title": "Combined",
                        "source_map": [{"chapter_start": 1, "chapter_end": 2}],
                        "recap_text": "",
                    }
                ],
            }
        )
        novel = imported["novel"]
        revision = novel["current_revision"]

        self.assertEqual([item["ordinal"] for item in revision["chapters"]], [1, 2])
        episode = revision["episodes"][0]
        updated = self.catalog.save_episode(
            {
                "id": episode["id"],
                "revision_id": revision["id"],
                "ordinal": 1,
                "title": "Combined edit",
                "source_map": episode["source_map"],
                "recap_text": "Previously, the door opened.",
                "expected_version": episode["row_version"],
            }
        )
        self.assertEqual(updated["recap_text"], "Previously, the door opened.")
        self.assertEqual(updated["row_version"], 2)
        with self.assertRaises(CatalogConflictError):
            self.catalog.save_episode(
                {
                    "id": episode["id"],
                    "revision_id": revision["id"],
                    "ordinal": 1,
                    "expected_version": 1,
                }
            )

    def test_novel_metadata_update_is_optimistically_versioned(self) -> None:
        novel = self.import_story()["novel"]
        updated = self.catalog.save_novel(
            {
                "id": novel["id"],
                "title": "Renamed",
                "synopsis": "Updated synopsis",
                "expected_version": novel["row_version"],
            }
        )
        self.assertEqual(updated["title"], "Renamed")
        self.assertEqual(updated["row_version"], novel["row_version"] + 1)
        with self.assertRaises(CatalogConflictError):
            self.catalog.save_novel(
                {
                    "id": novel["id"],
                    "title": "Stale edit",
                    "expected_version": novel["row_version"],
                }
            )

    def test_voice_state_write_is_strictly_limited_to_production_fields(self) -> None:
        novel = self.import_story()["novel"]
        managed = self.catalog.save_novel(
            {
                "id": novel["id"],
                "title": "Managed title",
                "synopsis": "Managed synopsis",
                "cover_path": "managed-cover.jpg",
                "metadata": {"editorial_note": "must survive"},
            }
        )
        candidate = {
            "profile": "dramatic",
            "label": "Dramatic",
            "provider": "local_kokoro",
            "voice_id": "af_heart",
            "audio_path": "hub://attachments/voice-previews/heart.wav",
            "audio_uri": "",
            "duration_seconds": 4.2,
            "excerpt": "The phone rang at midnight.",
            "language": "en",
            "voice_name": "Heart",
            "selection_key": "stable-voice-selection-key",
        }

        with_candidates = self.catalog.save_novel_voice_state(
            novel["id"], {"voice_candidates": [candidate]}
        )
        locked = self.catalog.save_novel_voice_state(
            novel["id"],
            {
                "locked_voice_provider": "local_kokoro",
                "locked_voice_id": "af_heart",
                "locked_voice_label": "Dramatic",
                "locked_voice_profile": "dramatic",
                "voice_lock_history": [],
            },
        )

        self.assertEqual(
            with_candidates["metadata"]["voice_candidates"][0]["voice_id"],
            "af_heart",
        )
        self.assertEqual(
            with_candidates["metadata"]["voice_candidates"][0]["selection_key"],
            "stable-voice-selection-key",
        )
        self.assertEqual(locked["metadata"]["locked_voice_id"], "af_heart")
        self.assertEqual(locked["metadata"]["editorial_note"], "must survive")
        self.assertEqual(locked["title"], managed["title"])
        self.assertEqual(locked["synopsis"], managed["synopsis"])
        self.assertEqual(locked["cover_path"], managed["cover_path"])

        with self.assertRaisesRegex(CatalogValidationError, "unsupported fields"):
            self.catalog.save_novel_voice_state(
                novel["id"],
                {"metadata": {"editorial_note": "producer overwrite"}},
            )
        with self.assertRaisesRegex(CatalogValidationError, "unsupported fields"):
            self.catalog.save_novel_voice_state(
                novel["id"],
                {
                    "voice_candidates": [
                        {
                            "provider": "local_kokoro",
                            "voice_id": "af_bella",
                            "title": "producer overwrite",
                        }
                    ]
                },
            )

    def test_voice_state_still_accepts_candidates_from_older_clients(self) -> None:
        novel = self.import_story()["novel"]

        saved = self.catalog.save_novel_voice_state(
            novel["id"],
            {
                "voice_candidates": [
                    {
                        "profile": "warm",
                        "label": "Warm",
                        "provider": "local_kokoro",
                        "voice_id": "af_bella",
                    }
                ]
            },
        )

        candidate = saved["metadata"]["voice_candidates"][0]
        self.assertEqual(candidate["voice_id"], "af_bella")
        self.assertEqual(candidate["selection_key"], "")


class BindingAndPromoCodeTests(CatalogTestCase):
    def test_platform_profile_can_preserve_a_legacy_id_and_templates(self) -> None:
        platform = self.catalog.save_platform(
            {
                "id": "legacy-platform-id",
                "name": "GoodNovel",
                "search_template": "Search {platform}: {code}",
                "ending_template": "Use {code} on {platform}.",
                "logo_path": "hub://attachments/platform-assets/goodnovel.png",
                "brand_color": "#D92D20",
            }
        )
        updated = self.catalog.save_platform(
            {
                "id": platform["id"],
                "name": "GoodNovel",
                "search_template": "Find {code}",
                "ending_template": platform["ending_template"],
                "expected_version": platform["row_version"],
            }
        )

        self.assertEqual(updated["id"], "legacy-platform-id")
        self.assertEqual(updated["search_template"], "Find {code}")
        self.assertEqual(
            updated["logo_path"],
            "hub://attachments/platform-assets/goodnovel.png",
        )
        self.assertEqual(updated["brand_color"], "#D92D20")
        listed = self.catalog.list_platforms()
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"][0]["brand_color"], "#D92D20")

    def test_binding_upserts_platform_and_exposes_five_historical_slots(self) -> None:
        novel = self.import_story()["novel"]
        first = self.bind_story(novel["id"])
        second = self.catalog.save_novel_binding(
            {
                "novel_id": novel["id"],
                "platform_name": "goodnovel",
                "platform_title": "Updated platform title",
            }
        )

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(self.catalog.list_platforms()["total"], 1)
        self.assertEqual(second["platform_title"], "Updated platform title")
        self.assertEqual(second["promo_code_slots_remaining"], 5)

    def test_promo_code_history_is_capped_and_admin_delete_is_a_hidden_tombstone(self) -> None:
        novel = self.import_story()["novel"]
        binding = self.bind_story(novel["id"])
        codes = [self.add_code(binding["id"], f"CODE{index}") for index in range(1, 6)]

        updated = self.catalog.update_promo_code(
            codes[0]["id"], {"status": "expired", "notes": "Used up"}
        )
        self.assertEqual(updated["status"], "expired")
        listed = self.catalog.list_promo_codes(binding["id"])
        self.assertEqual(listed["historical_count"], 5)
        self.assertEqual(listed["slots_remaining"], 0)
        with self.assertRaises(PromoCodeLimitError):
            self.add_code(binding["id"], "SIXTH")
        with self.assertRaisesRegex(CatalogValidationError, "immutable"):
            self.catalog.update_promo_code(codes[0]["id"], {"code": "CHANGED"})

        removed = self.catalog.delete_promo_code(codes[0]["id"])
        self.assertTrue(removed["deleted_at"])
        visible = self.catalog.list_promo_codes(binding["id"])
        self.assertEqual(visible["total"], 4)
        self.assertEqual(visible["historical_count"], 5)
        self.assertEqual(visible["slots_remaining"], 0)
        self.assertEqual(
            self.catalog.list_promo_codes(
                binding["id"], include_deleted=True
            )["total"],
            5,
        )

        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot be deleted"):
                connection.execute("DELETE FROM promo_codes WHERE id = ?", (codes[0]["id"],))
        finally:
            connection.close()

    def test_concurrent_code_claims_still_stop_at_five(self) -> None:
        novel = self.import_story()["novel"]
        binding = self.bind_story(novel["id"])

        def claim(index: int) -> str:
            try:
                self.add_code(binding["id"], f"PAR{index}")
                return "created"
            except PromoCodeLimitError:
                return "limited"

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(claim, range(8)))

        self.assertEqual(outcomes.count("created"), 5)
        self.assertEqual(outcomes.count("limited"), 3)
        self.assertEqual(
            self.catalog.list_promo_codes(binding["id"])["historical_count"], 5
        )

    def test_admin_delete_archives_platform_and_novel_without_erasing_history(self) -> None:
        first_novel = self.import_story(title="First Story")["novel"]
        first_binding = self.bind_story(first_novel["id"], "GoodNovel")
        first_code = self.add_code(first_binding["id"], "KEEP01")
        removed_platform = self.catalog.delete_platform(first_binding["platform_id"])

        self.assertTrue(removed_platform["archived"])
        self.assertEqual(self.catalog.list_platforms()["total"], 0)
        self.assertEqual(
            self.catalog.list_promo_codes(first_binding["id"])["items"][0]["status"],
            "revoked",
        )
        self.assertEqual(
            self.catalog.list_promo_codes(
                first_binding["id"], include_deleted=True
            )["items"][0]["id"],
            first_code["id"],
        )

        second_novel = self.import_story(title="Second Story", body="Different body.")[
            "novel"
        ]
        removed_novel = self.catalog.delete_novel(second_novel["id"])
        self.assertTrue(removed_novel["archived"])
        self.assertNotIn(
            second_novel["id"],
            {item["id"] for item in self.catalog.list_novels()["items"]},
        )


class AccountsDraftsAndPermissionsTests(CatalogTestCase):
    def test_first_user_must_be_an_active_administrator(self) -> None:
        with self.assertRaisesRegex(CatalogValidationError, "first software user"):
            self.catalog.save_user({"username": "producer-first", "role": "producer"})
        with self.assertRaisesRegex(CatalogValidationError, "first software user"):
            self.catalog.save_user(
                {"username": "inactive-first", "role": "admin", "active": False}
            )

        admin = self.catalog.save_user(
            {"username": "valid-first", "role": "admin", "active": True}
        )
        self.assertEqual(admin["role"], "admin")
        self.assertTrue(admin["active"])
        self.assertEqual(self.catalog.list_users()["total"], 1)

    def test_last_super_admin_cannot_be_disabled_demoted_or_overridden(self) -> None:
        admin = self.catalog.save_user(
            {"username": "only-admin", "role": "admin"}
        )
        for change in (
            {"active": False},
            {"role": "producer"},
        ):
            with self.subTest(change=change):
                with self.assertRaisesRegex(CatalogConflictError, "last active"):
                    self.catalog.save_user(
                        {"id": admin["id"], "username": admin["username"], **change}
                    )
        for permission in ("users.manage", "permissions.manage", "hub.manage"):
            with self.subTest(permission=permission):
                with self.assertRaisesRegex(CatalogConflictError, "last active"):
                    self.catalog.set_user_permission(admin["id"], permission, False)

        current = self.catalog.get_effective_permissions(admin["id"])
        self.assertTrue(current["active"])
        self.assertEqual(current["role"], "admin")
        self.assertTrue(
            all(current["effective"][permission] for permission in (
                "users.manage",
                "permissions.manage",
                "hub.manage",
            ))
        )

        second = self.catalog.save_user(
            {"username": "second-admin", "role": "admin"}
        )
        denied = self.catalog.set_user_permission(
            admin["id"], "hub.manage", False
        )
        self.assertFalse(denied["effective"]["hub.manage"])
        deactivated = self.catalog.save_user(
            {
                "id": admin["id"],
                "username": admin["username"],
                "active": False,
            }
        )
        self.assertFalse(deactivated["active"])
        self.assertTrue(self.catalog.get_effective_permissions(second["id"])["active"])

    def test_publishing_account_upserts_case_insensitively(self) -> None:
        first = self.catalog.save_publishing_account(
            {"network": "TikTok", "handle": "@StoryOne", "display_name": "One"}
        )
        second = self.catalog.save_publishing_account(
            {"network": "tiktok", "handle": "@storyone", "display_name": "Updated"}
        )

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["display_name"], "Updated")
        self.assertEqual(self.catalog.list_publishing_accounts()["total"], 1)

    def test_admin_delete_hides_publishing_account_but_keeps_archive(self) -> None:
        account = self.catalog.save_publishing_account(
            {"network": "TikTok", "handle": "@archive-me"}
        )
        removed = self.catalog.delete_publishing_account(account["id"])

        self.assertEqual(removed["status"], "archived")
        self.assertEqual(self.catalog.list_publishing_accounts()["total"], 0)
        archived = self.catalog.list_publishing_accounts(include_archived=True)
        self.assertEqual(archived["total"], 1)
        self.assertEqual(archived["items"][0]["id"], account["id"])

    def test_draft_allows_unassigned_account_and_freezes_code_snapshot(self) -> None:
        novel, binding, code, draft = self.make_draft(creative_line_count=3)

        self.assertIsNone(draft["publishing_account_id"])
        self.assertEqual(draft["creative_line_count"], 3)
        self.assertEqual(draft["promo_code_snapshot"], code["code"])
        self.assertEqual(draft["novel_title_snapshot"], novel["title"])
        self.assertEqual(len(draft["episode_ids"]), 1)
        self.catalog.update_promo_code(code["id"], {"status": "expired"})
        account = self.catalog.save_publishing_account(
            {"network": "TikTok", "handle": "series-account"}
        )
        updated = self.catalog.save_draft(
            {
                "id": draft["id"],
                "publishing_account_id": account["id"],
                "variant_count": 10,
                "expected_version": draft["row_version"],
            }
        )

        self.assertEqual(updated["publishing_account_id"], account["id"])
        self.assertEqual(updated["creative_line_count"], 10)
        self.assertEqual(updated["promo_code_snapshot"], code["code"])
        with self.assertRaisesRegex(CatalogValidationError, "frozen"):
            self.catalog.save_draft(
                {"id": updated["id"], "promo_code_id": "another-code"}
            )
        self.assertEqual(self.catalog.list_drafts(only_unassigned=True)["total"], 0)

    def test_draft_creative_line_count_is_positive_without_ten_video_cap(self) -> None:
        novel = self.import_story()["novel"]
        binding = self.bind_story(novel["id"])
        code = self.add_code(binding["id"])
        base = {
            "novel_id": novel["id"],
            "binding_id": binding["id"],
            "promo_code_id": code["id"],
        }
        with self.assertRaises(CatalogValidationError):
            self.catalog.save_draft({**base, "creative_line_count": 0})
        draft = self.catalog.save_draft(
            {**base, "creative_line_count": 25}
        )
        self.assertEqual(draft["creative_line_count"], 25)
        record = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "variant-25",
                "variant_index": 25,
            }
        )
        self.assertEqual(record["variant_index"], 25)

    def test_draft_rejects_episode_from_another_novel(self) -> None:
        first = self.import_story(title="First", body="First body.")["novel"]
        second = self.import_story(title="Second", body="Second body.")["novel"]
        binding = self.bind_story(first["id"])
        code = self.add_code(binding["id"])
        wrong_episode = second["current_revision"]["episodes"][0]["id"]
        with self.assertRaisesRegex(CatalogValidationError, "draft novel"):
            self.catalog.save_draft(
                {
                    "novel_id": first["id"],
                    "binding_id": binding["id"],
                    "promo_code_id": code["id"],
                    "creative_line_count": 1,
                    "episode_ids": [wrong_episode],
                }
            )
        self.assertEqual(self.catalog.list_drafts()["total"], 0)

    def test_role_defaults_and_per_user_allow_deny_overrides(self) -> None:
        admin = self.catalog.save_user(
            {
                "username": "owner",
                "display_name": "Owner",
                "role": "admin",
                "password_hash": "opaque-scrypt-hash",
            }
        )
        producer = self.catalog.save_user(
            {"username": "maker", "role": "producer"}
        )

        self.assertTrue(admin["has_password"])
        self.assertNotIn("password_hash", admin)
        admin_permissions = self.catalog.get_effective_permissions(admin["id"])
        producer_permissions = self.catalog.get_effective_permissions(producer["id"])
        self.assertTrue(admin_permissions["effective"]["hub.manage"])
        self.assertFalse(producer_permissions["effective"]["library.edit"])
        with self.assertRaisesRegex(CatalogValidationError, "admin or producer"):
            self.catalog.save_user(
                {"username": "team-lead", "role": "supervisor"}
            )
        allowed = self.catalog.set_user_permission(
            producer["id"], "library.edit", True, actor_user_id=admin["id"]
        )
        denied = self.catalog.set_user_permission(
            producer["id"], "records.view_own", False, actor_user_id=admin["id"]
        )
        self.assertTrue(allowed["effective"]["library.edit"])
        self.assertFalse(denied["effective"]["records.view_own"])
        reset = self.catalog.set_user_permission(
            producer["id"], "library.edit", None, actor_user_id=admin["id"]
        )
        self.assertFalse(reset["effective"]["library.edit"])


class ProductionRecordsMediaAndAuditTests(CatalogTestCase):
    def test_real_version_ten_shape_bootstraps_before_new_ledger_indexes(self) -> None:
        """A v10 table lacks batch/trash columns when the bootstrap first runs."""

        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DROP TABLE production_records")
            connection.execute(
                """
                CREATE TABLE production_records (
                    id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
                    draft_id TEXT REFERENCES production_drafts(id) ON DELETE SET NULL,
                    job_id TEXT,
                    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE RESTRICT,
                    binding_id TEXT NOT NULL REFERENCES novel_platform_bindings(id) ON DELETE RESTRICT,
                    episode_id TEXT REFERENCES episodes(id) ON DELETE SET NULL,
                    publishing_account_id TEXT REFERENCES publishing_accounts(id) ON DELETE SET NULL,
                    created_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
                    device_id TEXT NOT NULL DEFAULT '',
                    variant_index INTEGER NOT NULL CHECK (variant_index BETWEEN 1 AND 10),
                    novel_title_snapshot TEXT NOT NULL,
                    platform_name_snapshot TEXT NOT NULL,
                    promo_code_snapshot TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    output_path TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT,
                    completed_at TEXT,
                    lease_owner_device TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT,
                    heartbeat_at TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    archived_at TEXT,
                    archived_by_user_id TEXT REFERENCES software_users(id) ON DELETE SET NULL,
                    archive_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    row_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(site_id, job_id)
                )
                """
            )
            connection.execute("DELETE FROM schema_migrations WHERE version = 11")
            connection.commit()
        finally:
            connection.close()

        migrated = CatalogRepository(
            self.database_path, site_id="test-site", site_name="Test Studio"
        )
        with migrated._read_connection() as read_connection:
            columns = {
                str(row["name"])
                for row in read_connection.execute(
                    "PRAGMA table_info(production_records)"
                ).fetchall()
            }
            indexes = {
                str(row["name"])
                for row in read_connection.execute(
                    "PRAGMA index_list(production_records)"
                ).fetchall()
            }
        self.assertIn("batch_id", columns)
        self.assertIn("trashed_at", columns)
        self.assertIn("idx_records_batch_created", indexes)
        self.assertIn("idx_records_trash", indexes)

    def test_version_eleven_rebuilds_old_count_checks_without_losing_records(self) -> None:
        _novel, _binding, _code, draft = self.make_draft(creative_line_count=3)
        record = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "legacy-v10-job",
                "variant_index": 3,
                "status": "failed",
                "error_message": "legacy failure",
            }
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            for table, check_before, check_after in (
                (
                    "production_drafts",
                    "CHECK (creative_line_count > 0)",
                    "CHECK (creative_line_count BETWEEN 1 AND 10)",
                ),
                (
                    "production_records",
                    "CHECK (variant_index > 0)",
                    "CHECK (variant_index BETWEEN 1 AND 10)",
                ),
            ):
                sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone()[0]
                )
                legacy_table = f"{table}_legacy_v10"
                legacy_sql = sql.replace(
                    f"CREATE TABLE {table}", f"CREATE TABLE {legacy_table}", 1
                ).replace(check_before, check_after, 1)
                self.assertIn(check_after, legacy_sql)
                connection.execute(legacy_sql)
                columns = [
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})")
                ]
                column_list = ", ".join(columns)
                connection.execute(
                    f"INSERT INTO {legacy_table}({column_list}) SELECT {column_list} FROM {table}"
                )
                connection.execute(f"DROP TABLE {table}")
                connection.execute(f"ALTER TABLE {legacy_table} RENAME TO {table}")
            connection.execute("DELETE FROM schema_migrations WHERE version = 11")
            connection.commit()
        finally:
            connection.close()

        migrated = CatalogRepository(
            self.database_path, site_id="test-site", site_name="Test Studio"
        )
        self.assertEqual(migrated.get_record(record["id"])["error_message"], "legacy failure")
        updated = migrated.save_draft(
            {"id": draft["id"], "creative_line_count": 25}
        )
        self.assertEqual(updated["creative_line_count"], 25)
        with migrated._read_connection() as read_connection:
            draft_sql = str(
                read_connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'production_drafts'"
                ).fetchone()[0]
            )
            record_sql = str(
                read_connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'production_records'"
                ).fetchone()[0]
            )
            self.assertNotIn("BETWEEN 1 AND 10", draft_sql)
            self.assertNotIn("BETWEEN 1 AND 10", record_sql)
            self.assertEqual(read_connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_record_groups_preserve_batches_and_retry_attempt_history(self) -> None:
        _novel, _binding, _code, draft = self.make_draft(creative_line_count=25)
        run_id = "run-shared-001"
        first = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "batch-job-01",
                "variant_index": 1,
                "status": "failed",
                "error_message": "first provider failed",
                "device_id": "worker-a",
                "metadata": {
                    "production_run_id": run_id,
                    "job_snapshot": {
                        "id": "batch-job-01",
                        "batch_id": draft["id"],
                    },
                },
            }
        )
        second = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "batch-job-02",
                "variant_index": 25,
                "status": "completed",
                "progress": 1,
                "device_id": "worker-a",
                "metadata": {"production_run_id": run_id},
            }
        )
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertNotEqual(first["batch_id"], draft["id"])
        self.assertEqual(
            first["metadata"]["job_snapshot"]["batch_id"],
            first["batch_id"],
        )
        self.assertEqual(
            first["metadata"]["job_snapshot"]["production_record_id"],
            first["id"],
        )
        projected = self.catalog.save_production_record(
            {
                "id": first["id"],
                "status": "failed",
                "metadata": {"stage_label": "render failed"},
            }
        )
        self.assertEqual(
            projected["metadata"]["job_snapshot"]["batch_id"],
            first["batch_id"],
        )
        self.assertEqual(
            projected["metadata"]["job_snapshot"]["production_record_id"],
            first["id"],
        )

        retried = self.catalog.begin_record_retry(first["id"])
        self.assertEqual(retried["current_attempt"], 2)
        completed = self.catalog.save_production_record(
            {"id": first["id"], "status": "completed", "progress": 1}
        )
        self.assertEqual(completed["status"], "completed")
        attempts = self.catalog.get_record(first["id"])["attempts"]
        self.assertEqual([item["attempt_no"] for item in attempts], [2, 1])
        self.assertEqual(attempts[0]["status"], "completed")
        self.assertEqual(attempts[1]["status"], "failed")
        self.assertEqual(attempts[1]["error_message"], "first provider failed")

        grouped = self.catalog.list_record_groups(batch_id=first["batch_id"])
        self.assertEqual(grouped["total_records"], 2)
        self.assertEqual(len(grouped["items"]), 1)
        self.assertEqual(len(grouped["items"][0]["batches"]), 1)
        tasks = grouped["items"][0]["batches"][0]["tasks"]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(grouped["summary"]["completed"], 2)

    def test_begin_record_retry_can_atomically_claim_the_new_attempt(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        record = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "atomic-retry-claim",
                "status": "failed",
                "error_message": "first attempt failed",
            }
        )

        retried = self.catalog.begin_record_retry(
            record["id"],
            device_id="worker-retry",
            lease_seconds=60,
        )

        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["current_attempt"], 2)
        self.assertEqual(retried["lease_owner_device"], "worker-retry")
        self.assertGreater(retried["lease_generation"], 0)
        self.assertTrue(retried["lease_expires_at"])
        updated = self.catalog.save_production_record(
            {
                "id": record["id"],
                "expected_lease_owner_device": "worker-retry",
                "expected_lease_generation": retried["lease_generation"],
                "status": "running",
                "progress": 0.25,
            }
        )
        self.assertEqual(updated["status"], "running")

    def test_begin_record_retry_does_not_reopen_an_actively_leased_record(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        record = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "atomic-retry-conflict",
                "status": "failed",
            }
        )
        with self.catalog._write_connection() as connection:
            connection.execute(
                """
                UPDATE production_records
                SET lease_owner_device = 'stale-process',
                    lease_generation = lease_generation + 1,
                    lease_expires_at = '2999-01-01T00:00:00+00:00'
                WHERE id = ?
                """,
                (record["id"],),
            )

        with self.assertRaisesRegex(CatalogConflictError, "actively leased"):
            self.catalog.begin_record_retry(
                record["id"],
                device_id="worker-retry",
                lease_seconds=60,
            )

        unchanged = self.catalog.get_record(record["id"])
        self.assertEqual(unchanged["status"], "failed")
        self.assertEqual(unchanged["current_attempt"], 1)

    def test_durable_batch_summary_excludes_gate_and_uses_terminal_progress(self) -> None:
        _novel, _binding, _code, draft = self.make_draft(creative_line_count=10)
        gate = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "device_id": "worker-a",
                "status": "queued",
                "metadata": {"lease_gate": True, "draft_id": draft["id"]},
            }
        )
        statuses = (("running", 0.4), ("completed", 1), ("failed", 0.2), ("cancelled", 0))
        records = [
            self.catalog.save_production_record(
                {
                    "draft_id": draft["id"],
                    "job_id": f"summary-job-{index}",
                    "variant_index": index,
                    "device_id": "worker-a",
                    "status": status,
                    "progress": progress,
                    "metadata": {"production_run_id": "summary-run"},
                }
            )
            for index, (status, progress) in enumerate(statuses, start=1)
        ]
        batch_id = str(records[0]["batch_id"])
        bound = self.catalog.bind_lease_gate_batch(gate["id"], batch_id)
        self.assertIsNone(bound["batch_id"])
        self.assertEqual(bound["metadata"]["durable_batch_id"], batch_id)

        summary = self.catalog.get_production_batch_summaries([batch_id])["items"][batch_id]
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["active"], 1)
        self.assertEqual(summary["unfinished"], 1)
        self.assertEqual(summary["queued"], 0)
        self.assertEqual(summary["running"], 1)
        self.assertEqual(summary["approval"], 0)
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["cancelled"], 1)
        self.assertAlmostEqual(summary["overall_progress"], 0.85)

    def test_active_gate_lookup_is_not_limited_by_500_newer_records(self) -> None:
        _novel, _binding, _code, draft = self.make_draft(creative_line_count=600)
        gate = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "device_id": "worker-501",
                "status": "queued",
                "metadata": {"lease_gate": True, "draft_id": draft["id"]},
            }
        )
        self.catalog.claim_record_lease(gate["id"], "worker-501", lease_seconds=180)
        base = {
            "draft_id": draft["id"],
            "device_id": "worker-501",
            "status": "completed",
            "progress": 1,
        }
        first_page = [
            {
                **base,
                "job_id": f"filler-{index:04d}",
                "variant_index": index,
                "metadata": {"production_run_id": "filler-run"},
            }
            for index in range(1, 501)
        ]
        self.catalog.save_production_records_bulk(first_page)
        self.catalog.save_production_record(
            {
                **base,
                "job_id": "filler-0501",
                "variant_index": 501,
                "metadata": {"production_run_id": "filler-run"},
            }
        )

        found = self.catalog.find_active_draft_gate(
            draft["id"], active_at="2000-01-01T00:00:00+00:00"
        )["item"]
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], gate["id"])
        reconciliation = self.catalog.list_reconciliation_records("worker-501")
        self.assertEqual(reconciliation["total"], 1)
        self.assertEqual(reconciliation["items"][0]["id"], gate["id"])

    def test_cancel_trace_cannot_be_resurrected_by_late_worker_update(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        record = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "cancel-now",
                "status": "running",
                "progress": 0.4,
            }
        )
        cancelled = self.catalog.request_record_cancellation(
            [record["id"]], reason="operator stopped batch"
        )
        self.assertEqual(cancelled["cancelled"], [record["id"]])
        late = self.catalog.save_production_record(
            {
                "id": record["id"],
                "status": "completed",
                "progress": 1,
                "output_path": r"D:\employee-output\late.mp4",
            }
        )
        self.assertEqual(late["status"], "cancelled")
        self.assertEqual(late["progress"], 0.4)
        self.assertEqual(late["output_path"], "")
        self.assertTrue(late["cancel_requested_at"])
        self.assertTrue(late["cancelled_at"])
        self.assertEqual(late["cancellation_reason"], "operator stopped batch")
        self.assertEqual(
            self.catalog.list_record_groups(status="cancelled")["total_records"],
            1,
        )

    def test_recycle_bin_requires_terminal_record_and_hard_delete_is_metadata_only(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        active = self.catalog.save_production_record(
            {"draft_id": draft["id"], "job_id": "active-trash", "status": "running"}
        )
        with self.assertRaises(CatalogConflictError):
            self.catalog.trash_production_records([active["id"]])
        failed = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "failed-trash",
                "status": "failed",
                "output_path": r"D:\employee-output\failed.mp4",
            }
        )
        self.catalog.trash_production_records([failed["id"]])
        self.assertEqual(self.catalog.list_records()["total"], 1)
        self.assertEqual(self.catalog.list_records(trashed=True)["total"], 1)
        self.catalog.restore_trashed_records([failed["id"]])
        self.assertFalse(self.catalog.get_record(failed["id"])["trashed"])
        self.catalog.trash_production_records([failed["id"]])
        deleted = self.catalog.delete_trashed_records([failed["id"]])
        self.assertFalse(deleted["local_files_deleted"])
        with self.assertRaises(LookupError):
            self.catalog.get_record(failed["id"])

    def test_job_archive_persists_snapshot_and_restores_without_deleting_artifacts(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        created = self.catalog.save_production_record(
            {"draft_id": draft["id"], "job_id": "archive-job", "status": "failed"}
        )
        artifact = self.catalog.add_artifact(
            {
                "record_id": created["id"],
                "kind": "error_log",
                "local_path": r"D:\output\job-error.log",
                "sha256": "b" * 64,
                "size_bytes": 42,
            }
        )
        snapshot = {
            "id": "archive-job",
            "batch_id": draft["id"],
            "status": "failed",
            "platform_id": "platform-1",
            "title": "A Quiet Door",
            "error_log": artifact["local_path"],
            "output_file": r"D:\output\partial.mp4",
        }

        archived = self.catalog.archive_job_snapshot("archive-job", snapshot)
        self.assertTrue(archived["job"]["archived"])
        self.assertEqual(archived["job"]["batch_id"], created["batch_id"])
        self.assertEqual(archived["job"]["production_record_id"], created["id"])
        self.assertTrue(self.catalog.get_record(created["id"])["archived"])

        reopened = CatalogRepository(
            self.database_path,
            site_id="test-site",
            site_name="Test Studio",
            busy_timeout_ms=3000,
        )
        persisted = reopened.get_archived_job("archive-job")
        self.assertEqual(persisted["error_log"], artifact["local_path"])
        self.assertEqual(persisted["batch_id"], created["batch_id"])
        self.assertEqual(persisted["production_record_id"], created["id"])
        self.assertEqual(reopened.list_archived_jobs()["total"], 1)
        self.assertEqual(reopened.get_record(created["id"])["artifact_count"], 1)

        restored = reopened.restore_job_snapshot("archive-job")
        self.assertFalse(restored["job"]["archived"])
        self.assertEqual(restored["job"]["batch_id"], created["batch_id"])
        self.assertEqual(restored["job"]["production_record_id"], created["id"])
        record = reopened.get_record(created["id"])
        self.assertFalse(record["archived"])
        self.assertEqual(record["artifacts"][0]["sha256"], "b" * 64)
        self.assertEqual(reopened.list_archived_jobs()["total"], 0)

    def test_job_archive_rejects_non_terminal_record(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        self.catalog.save_production_record(
            {"draft_id": draft["id"], "job_id": "active-job", "status": "running"}
        )
        with self.assertRaisesRegex(CatalogConflictError, "finished"):
            self.catalog.archive_job_snapshot(
                "active-job", {"id": "active-job", "status": "rendering"}
            )

    def test_batch_archive_and_restore_are_atomic_and_idempotent(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        run_id = "archive-batch-run"
        first = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "archive-batch-01",
                "status": "completed",
                "metadata": {"production_run_id": run_id},
            }
        )
        second = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "archive-batch-02",
                "status": "failed",
                "metadata": {"production_run_id": run_id},
            }
        )
        self.assertEqual(first["batch_id"], second["batch_id"])
        snapshots = [
            {
                "id": record["job_id"],
                "batch_id": record["batch_id"],
                "platform_id": "platform-1",
                "status": record["status"],
            }
            for record in (first, second)
        ]

        archived = self.catalog.archive_batch_snapshots(
            first["batch_id"], snapshots
        )
        self.assertEqual(archived["changed_count"], 2)
        self.assertEqual(set(archived["job_ids"]), {"archive-batch-01", "archive-batch-02"})
        self.assertTrue(self.catalog.get_record(first["id"])["archived"])
        self.assertTrue(self.catalog.get_record(second["id"])["archived"])

        repeated = self.catalog.archive_batch_snapshots(first["batch_id"], [])
        self.assertTrue(repeated["already_archived"])
        self.assertEqual(repeated["changed_count"], 0)
        self.assertEqual(repeated["archived_count"], 2)

        restored = self.catalog.restore_batch_snapshots(first["batch_id"])
        self.assertEqual(restored["restored_count"], 2)
        self.assertFalse(self.catalog.get_record(first["id"])["archived"])
        self.assertFalse(self.catalog.get_record(second["id"])["archived"])
        repeated_restore = self.catalog.restore_batch_snapshots(first["batch_id"])
        self.assertTrue(repeated_restore["already_restored"])
        self.assertEqual(repeated_restore["restored_count"], 0)

    def test_batch_archive_rejects_mixed_terminal_and_active_without_partial_write(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        run_id = "mixed-archive-run"
        finished = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "mixed-finished",
                "status": "failed",
                "metadata": {"production_run_id": run_id},
            }
        )
        active = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "mixed-active",
                "status": "running",
                "metadata": {"production_run_id": run_id},
            }
        )
        with self.assertRaisesRegex(CatalogConflictError, "every task is finished"):
            self.catalog.archive_batch_snapshots(
                finished["batch_id"],
                [
                    {"id": "mixed-finished", "status": "failed"},
                    {"id": "mixed-active", "status": "running"},
                ],
            )
        self.assertFalse(self.catalog.get_record(finished["id"])["archived"])
        self.assertFalse(self.catalog.get_record(active["id"])["archived"])

    def test_batch_archive_and_restore_reject_unknown_batch(self) -> None:
        with self.assertRaises(CatalogNotFoundError):
            self.catalog.archive_batch_snapshots("missing-batch", [])
        with self.assertRaises(CatalogNotFoundError):
            self.catalog.restore_batch_snapshots("missing-batch")

    def test_record_lease_claim_heartbeat_release_and_persistence(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        record = self.catalog.save_production_record(
            {"draft_id": draft["id"], "job_id": "lease-lifecycle"}
        )
        self.assertEqual(record["lease_owner_device"], "")
        self.assertIsNone(record["lease_expires_at"])
        self.assertIsNone(record["heartbeat_at"])

        claimed = self.catalog.claim_record_lease(
            record["id"], "worker-a", lease_seconds=60
        )
        self.assertTrue(claimed["claimed"])
        self.assertFalse(claimed["renewed"])
        self.assertEqual(claimed["record"]["lease_owner_device"], "worker-a")
        generation = claimed["record"]["lease_generation"]
        with self.assertRaisesRegex(CatalogConflictError, "another process"):
            self.catalog.claim_record_lease(record["id"], "worker-a")
        renewed = self.catalog.claim_record_lease(
            record["id"],
            "worker-a",
            lease_generation=generation,
            lease_seconds=60,
        )
        self.assertTrue(renewed["renewed"])
        self.assertEqual(renewed["record"]["lease_generation"], generation)
        with self.assertRaises(CatalogConflictError):
            self.catalog.claim_record_lease(record["id"], "worker-b")
        with self.assertRaises(CatalogConflictError):
            self.catalog.heartbeat_record_lease(record["id"], "worker-b")
        with self.assertRaises(CatalogConflictError):
            self.catalog.release_record_lease(record["id"], "worker-b")
        with self.assertRaisesRegex(CatalogConflictError, "generation is required"):
            self.catalog.heartbeat_record_lease(record["id"], "worker-a")
        with self.assertRaisesRegex(CatalogConflictError, "generation is required"):
            self.catalog.release_record_lease(record["id"], "worker-a")

        heartbeat = self.catalog.heartbeat_record_lease(
            record["id"],
            "worker-a",
            lease_generation=generation,
            lease_seconds=120,
        )
        self.assertTrue(heartbeat["heartbeat"])
        reopened = CatalogRepository(
            self.database_path,
            site_id="test-site",
            site_name="Test Studio",
            busy_timeout_ms=3000,
        )
        persisted = reopened.get_record(record["id"])
        self.assertEqual(persisted["lease_owner_device"], "worker-a")
        self.assertEqual(
            persisted["heartbeat_at"], heartbeat["record"]["heartbeat_at"]
        )

        released = reopened.release_record_lease(
            record["id"], "worker-a", lease_generation=generation
        )
        repeated = reopened.release_record_lease(record["id"], "worker-a")
        self.assertTrue(released["released"])
        self.assertFalse(repeated["released"])
        self.assertEqual(released["record"]["lease_owner_device"], "")
        self.assertIsNone(released["record"]["lease_expires_at"])

    def test_record_projection_requires_current_active_lease_owner(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        record = self.catalog.save_production_record(
            {"draft_id": draft["id"], "job_id": "lease-guarded-update"}
        )
        claimed = self.catalog.claim_record_lease(
            record["id"], "worker-a", lease_seconds=60
        )
        generation = claimed["record"]["lease_generation"]

        updated = self.catalog.save_production_record(
            {
                "id": record["id"],
                "expected_lease_owner_device": "worker-a",
                "expected_lease_generation": generation,
                "status": "running",
                "progress": 0.25,
            }
        )
        self.assertEqual(updated["status"], "running")
        with self.assertRaisesRegex(CatalogConflictError, "another device"):
            self.catalog.save_production_record(
                {
                    "id": record["id"],
                    "expected_lease_owner_device": "worker-b",
                    "expected_lease_generation": generation,
                    "status": "completed",
                    "progress": 1,
                }
            )
        with self.assertRaisesRegex(CatalogConflictError, "lease owner and generation"):
            self.catalog.save_production_record(
                {
                    "id": record["id"],
                    "status": "running",
                    "progress": 0.5,
                }
            )
        with self.catalog._write_connection() as connection:
            connection.execute(
                "UPDATE production_records SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", record["id"]),
            )
        with self.assertRaisesRegex(CatalogConflictError, "expired"):
            self.catalog.save_production_record(
                {
                    "id": record["id"],
                    "expected_lease_owner_device": "worker-a",
                    "expected_lease_generation": generation,
                    "status": "completed",
                    "progress": 1,
                }
            )

    def test_same_device_retry_fences_stale_worker_generation(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        record = self.catalog.save_production_record(
            {"draft_id": draft["id"], "job_id": "same-device-fencing"}
        )
        first = self.catalog.claim_record_lease(
            record["id"], "worker-a", lease_seconds=60
        )["record"]
        with self.catalog._write_connection() as connection:
            connection.execute(
                "UPDATE production_records SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", record["id"]),
            )
        second = self.catalog.claim_record_lease(
            record["id"], "worker-a", lease_seconds=60
        )["record"]
        self.assertGreater(second["lease_generation"], first["lease_generation"])

        with self.assertRaisesRegex(CatalogConflictError, "superseded"):
            self.catalog.save_production_record(
                {
                    "id": record["id"],
                    "expected_lease_owner_device": "worker-a",
                    "expected_lease_generation": first["lease_generation"],
                    "status": "running",
                    "progress": 0.2,
                }
            )
        updated = self.catalog.save_production_record(
            {
                "id": record["id"],
                "expected_lease_owner_device": "worker-a",
                "expected_lease_generation": second["lease_generation"],
                "status": "running",
                "progress": 0.3,
            }
        )
        self.assertEqual(updated["progress"], 0.3)

    def test_concurrent_record_claim_has_exactly_one_winner(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        record = self.catalog.save_production_record(
            {"draft_id": draft["id"], "job_id": "lease-race"}
        )

        def claim(index: int) -> tuple[str, str]:
            device = f"worker-{index}"
            try:
                result = self.catalog.claim_record_lease(record["id"], device)
                return "claimed", result["record"]["lease_owner_device"]
            except CatalogConflictError:
                return "conflict", device

        with ThreadPoolExecutor(max_workers=12) as executor:
            outcomes = list(executor.map(claim, range(12)))

        winners = [device for status, device in outcomes if status == "claimed"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(
            sum(status == "conflict" for status, _device in outcomes), 11
        )
        self.assertEqual(
            self.catalog.get_record(record["id"])["lease_owner_device"], winners[0]
        )

    def test_concurrent_same_device_process_claim_has_exactly_one_winner(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        record = self.catalog.save_production_record(
            {"draft_id": draft["id"], "job_id": "same-device-process-race"}
        )

        def claim(_index: int) -> str:
            try:
                self.catalog.claim_record_lease(record["id"], "shared-workstation")
                return "claimed"
            except CatalogConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=12) as executor:
            outcomes = list(executor.map(claim, range(12)))

        self.assertEqual(outcomes.count("claimed"), 1)
        self.assertEqual(outcomes.count("conflict"), 11)

    def test_expired_record_lease_can_be_reclaimed_by_another_device(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        record = self.catalog.save_production_record(
            {"draft_id": draft["id"], "job_id": "lease-expiry"}
        )
        self.catalog.claim_record_lease(record["id"], "worker-old")
        with self.catalog._write_connection() as connection:
            connection.execute(
                """
                UPDATE production_records
                SET lease_expires_at = '2000-01-01T00:00:00+00:00'
                WHERE id = ?
                """,
                (record["id"],),
            )

        reclaimed = self.catalog.claim_record_lease(
            record["id"], "worker-new", lease_seconds=30
        )
        self.assertTrue(reclaimed["reclaimed"])
        self.assertEqual(reclaimed["record"]["lease_owner_device"], "worker-new")
        with self.assertRaises(CatalogConflictError):
            self.catalog.heartbeat_record_lease(record["id"], "worker-old")

    def test_record_artifacts_filters_and_frozen_snapshots(self) -> None:
        account = self.catalog.save_publishing_account(
            {"network": "TikTok", "handle": "publish-here"}
        )
        novel, _binding, code, draft = self.make_draft(
            publishing_account_id=account["id"], creative_line_count=2
        )
        episode_id = draft["episode_ids"][0]
        record = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "job-001",
                "episode_id": episode_id,
                "variant_index": 2,
                "status": "queued",
                "device_id": "worker-a",
            }
        )
        self.catalog.update_promo_code(code["id"], {"status": "revoked"})
        completed = self.catalog.save_production_record(
            {
                "id": record["id"],
                "job_id": "job-001",
                "status": "completed",
                "progress": 1,
                "output_path": r"D:\output\final.mp4",
                "expected_version": record["row_version"],
            }
        )
        artifact = self.catalog.add_artifact(
            {
                "record_id": record["id"],
                "kind": "final_video",
                "device_id": "worker-a",
                "local_path": r"D:\output\final.mp4",
                "sha256": "a" * 64,
                "size_bytes": 1024,
                "duration_seconds": 90.5,
            }
        )

        self.assertEqual(completed["promo_code_snapshot"], code["code"])
        self.assertEqual(completed["publishing_account_id"], account["id"])
        self.assertIsNotNone(completed["completed_at"])
        self.assertEqual(artifact["kind"], "final_video")
        fetched = self.catalog.get_record(record["id"])
        self.assertEqual(fetched["artifact_count"], 1)
        self.assertEqual(fetched["artifacts"][0]["sha256"], "a" * 64)
        self.assertEqual(
            self.catalog.list_records(
                status="completed",
                novel_id=novel["id"],
                publishing_account_id=account["id"],
            )["total"],
            1,
        )
        with self.assertRaises(CatalogConflictError):
            self.catalog.save_production_record(
                {
                    "draft_id": draft["id"],
                    "job_id": "job-001",
                    "variant_index": 1,
                }
            )

    def test_media_usage_aggregates_events_and_all_mutations_are_audited(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        record = self.catalog.save_production_record(
            {"draft_id": draft["id"], "job_id": "media-job", "variant_index": 1}
        )
        self.catalog.record_media_usage(
            {
                "fingerprint": "video-sha256",
                "media_type": "video",
                "display_name": "clip.mp4",
                "record_id": record["id"],
                "device_id": "worker-a",
                "use_count": 1,
                "metadata": {"mirror": False},
            }
        )
        self.catalog.record_media_usage(
            {
                "fingerprint": "video-sha256",
                "media_type": "video",
                "display_name": "clip.mp4",
                "record_id": record["id"],
                "device_id": "worker-a",
                "use_count": 2,
                "metadata": {"mirror": True},
            }
        )

        usage = self.catalog.list_media_usage(fingerprint="video-sha256")
        self.assertEqual(usage["total"], 1)
        self.assertEqual(usage["items"][0]["total_uses"], 3)
        self.assertEqual(usage["items"][0]["event_count"], 2)
        audit = self.catalog.list_audit_events()
        actions = {item["action"] for item in audit["items"]}
        self.assertIn("novel.imported", actions)
        self.assertIn("draft.created", actions)
        self.assertIn("production_record.created", actions)
        self.assertIn("media_usage.recorded", actions)
        serialized_audit = str(audit)
        self.assertNotIn("Chapter one begins", serialized_audit)

    def test_progress_only_updates_do_not_expand_immutable_audit_history(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        record = self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "progress-telemetry-job",
                "variant_index": 1,
            }
        )

        for index in range(1, 100):
            updated = self.catalog.save_production_record(
                {
                    "id": record["id"],
                    "status": "queued",
                    "progress": index / 100,
                }
            )
        self.assertAlmostEqual(updated["progress"], 0.99)
        audit = self.catalog.list_audit_events(
            entity_type="production_record", limit=500
        )
        self.assertEqual(audit["total"], 1)
        self.assertEqual(audit["items"][0]["action"], "production_record.created")

        self.catalog.save_production_record(
            {"id": record["id"], "status": "running", "progress": 0.99}
        )
        self.catalog.save_production_record(
            {
                "id": record["id"],
                "status": "completed",
                "progress": 1,
                "output_path": r"D:\output\final.mp4",
            }
        )
        audit = self.catalog.list_audit_events(
            entity_type="production_record", limit=500
        )
        self.assertEqual(audit["total"], 3)
        self.assertEqual(
            {item["after"]["status"] for item in audit["items"]},
            {"queued", "running", "completed"},
        )

    def test_bootstrap_counts_failures_without_loading_record_bodies(self) -> None:
        _novel, _binding, _code, draft = self.make_draft()
        self.catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "failed-job",
                "variant_index": 1,
                "status": "failed",
                "error_message": "synthetic failure",
            }
        )
        summary = self.catalog.bootstrap_summary()
        self.assertEqual(summary["counts"]["records"], 1)
        self.assertEqual(summary["counts"]["failed_records"], 1)

    def test_import_detects_language_and_supports_server_side_filtering(self) -> None:
        body = (
            "Anoche sonó el teléfono cuando estaba a punto de dormir. La mujer "
            "dijo que conocía a mi marido y preguntó por qué nunca había visto la "
            "habitación cerrada. Pensé que estaba mintiendo, pero después miré "
            "dentro del escritorio y todo cambió."
        )
        imported = self.catalog.import_novel(
            {"title": "Not a language hint", "body": body, "language": "auto"}
        )["novel"]

        self.assertEqual(imported["language"], "es")
        self.assertEqual(imported["language_name"], "西班牙语")
        self.assertEqual(imported["language_source"], "auto")
        self.assertGreater(imported["language_confidence"], 0.64)
        self.assertEqual(imported["language_detection"]["code"], "es")
        self.assertEqual(self.catalog.list_novels(language_code="es")["total"], 1)
        self.assertEqual(self.catalog.list_novels(language_code="en")["total"], 0)

    def test_missing_blank_and_auto_language_all_trigger_detection(self) -> None:
        base_body = (
            "Last night the phone rang while I was getting ready for bed. The woman "
            "said that she knew my husband, and then she asked why I had never noticed "
            "the locked room. I thought she was lying, but when I looked through his "
            "desk, everything changed. "
        )
        language_inputs = (None, "", "auto")
        for index, language in enumerate(language_inputs, start=1):
            payload = {
                "title": f"Automatic {index}",
                "body": base_body + ("Then she ended the call. " * index),
            }
            if language is not None:
                payload["language"] = language
            novel = self.catalog.import_novel(payload)["novel"]
            self.assertEqual(novel["language"], "en")
            self.assertEqual(novel["language_source"], "auto")

    def test_manual_language_survives_revisions_and_auto_redetect_restores_body_result(self) -> None:
        portuguese = (
            "Na noite passada, o telefone tocou quando eu estava prestes a dormir. "
            "A mulher disse que conhecia meu marido e perguntou por que eu nunca "
            "tinha visto o quarto fechado. Pensei que ela estava mentindo, porém "
            "depois olhei dentro da mesa e tudo mudou."
        )
        first = self.catalog.import_novel(
            {
                "title": "Manual classification",
                "body": portuguese,
                "language": "es-ES",
                "metadata": {"locked_voice_id": "voice-a", "tags": ["romance"]},
            }
        )["novel"]
        self.assertEqual(first["language"], "es")
        self.assertEqual(first["language_source"], "manual")
        self.assertEqual(first["language_detection"]["detected_code"], "pt")

        indonesian = (
            "Tadi malam telepon berbunyi ketika aku akan tidur. Seorang wanita "
            "berkata bahwa dia mengenal suamiku dan bertanya mengapa aku tidak "
            "pernah melihat kamar yang terkunci. Aku berpikir dia berbohong, "
            "tetapi kemudian aku melihat meja dan semuanya berubah."
        )
        revised = self.catalog.import_novel(
            {
                "novel_id": first["id"],
                "title": first["title"],
                "body": indonesian,
                "language": "auto",
                "metadata": {"source_word_count": 42},
            }
        )["novel"]
        self.assertEqual(revised["language"], "es")
        self.assertEqual(revised["language_source"], "manual")
        self.assertEqual(revised["language_detection"]["detected_code"], "id")
        self.assertEqual(
            revised["current_revision"]["metadata"]["language_detection"]["code"],
            "id",
        )
        self.assertEqual(revised["metadata"]["locked_voice_id"], "voice-a")
        self.assertEqual(revised["metadata"]["tags"], ["romance"])

        redetected = self.catalog.save_novel(
            {"id": first["id"], "redetect_language": True}
        )
        self.assertEqual(redetected["language"], "id")
        self.assertEqual(redetected["language_source"], "auto")

    def test_duplicate_import_repairs_detection_without_erasing_manual_override(self) -> None:
        body = (
            "La nuit dernière, le téléphone a sonné lorsque j'allais dormir. La "
            "femme a dit qu'elle connaissait mon mari et demanda pourquoi je "
            "n'avais jamais vu la pièce fermée. Je pensais qu'elle mentait, "
            "pourtant après avoir regardé dans le bureau, tout a changé."
        )
        first = self.catalog.import_novel(
            {"title": "Duplicate", "body": body, "language": "de"}
        )["novel"]
        with self.catalog._write_connection() as connection:
            connection.execute(
                """
                UPDATE novels SET detected_language_code = 'unknown',
                    detected_language_name = '未识别', detected_language_confidence = 0
                WHERE id = ?
                """,
                (first["id"],),
            )
        duplicate = self.catalog.import_novel(
            {"title": "Another title", "body": body}
        )
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(duplicate["novel"]["language"], "de")
        self.assertEqual(
            duplicate["novel"]["language_detection"]["detected_code"], "fr"
        )

    def test_version_four_catalog_backfills_language_from_current_body(self) -> None:
        spanish = (
            "Anoche sonó el teléfono cuando estaba a punto de dormir. La mujer "
            "dijo que conocía a mi marido y preguntó por qué nunca había visto la "
            "habitación cerrada. Pensé que estaba mintiendo, pero después miré "
            "dentro del escritorio y todo cambió."
        )
        novel = self.catalog.import_novel(
            {"title": "Legacy", "body": spanish}
        )["novel"]
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DROP INDEX IF EXISTS idx_novels_site_language")
            for column in (
                "language_detected_at",
                "detected_language_confidence",
                "detected_language_name",
                "detected_language_code",
                "language_source",
                "language_confidence",
                "language_name",
                "language_code",
            ):
                connection.execute(f"ALTER TABLE novels DROP COLUMN {column}")
            connection.execute(
                "UPDATE novels SET language = 'en-US' WHERE id = ?", (novel["id"],)
            )
            connection.execute("DELETE FROM schema_migrations WHERE version >= 5")
            connection.commit()
        finally:
            connection.close()

        migrated = CatalogRepository(
            self.database_path,
            site_id="test-site",
            site_name="Test Studio",
        )
        restored = migrated.get_novel(novel["id"])
        self.assertEqual(migrated.bootstrap_summary()["schema_version"], SCHEMA_VERSION)
        self.assertEqual(restored["language"], "es")
        self.assertEqual(restored["language_source"], "auto")
        self.assertEqual(restored["language_detection"]["detected_code"], "es")


if __name__ == "__main__":
    unittest.main()
