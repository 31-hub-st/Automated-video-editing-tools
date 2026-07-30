from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from storyforge.api import StoryForgeApi
from storyforge.config import AppSettings, SettingsRepository
from storyforge.desktop_bridge import (
    DESKTOP_RPC_PERMISSIONS,
    EMPLOYEE_DESKTOP_METHODS,
    StoryForgeDesktopBridge,
)
from storyforge.web import WEB_RPC_PERMISSIONS


class DesktopBridgeAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = SettingsRepository(Path(temporary.name))
        repository.save(AppSettings(), [], [])
        self.api = StoryForgeApi(repository=repository)
        self.addCleanup(self.api._shutdown)
        created = self.api.save_software_user(
            {
                "username": "employee-one",
                "display_name": "Employee One",
                "role": "producer",
                "active": True,
            }
        )
        self.assertTrue(created["ok"], created)
        self.employee_id = str(created["data"]["id"])
        self.bridge = StoryForgeDesktopBridge(self.api)

    def test_employee_cannot_mutate_shared_or_hub_administration(self) -> None:
        logged_in = self.bridge.desktop_login("employee-one", "xs123456")
        self.assertTrue(logged_in["ok"], logged_in)
        for method, args in (
            ("save_platform", [{"name": "Denied Platform"}]),
            ("import_novel_text", [{"title": "Denied Novel", "body": "text"}]),
            ("save_novel", [{"title": "Denied Novel"}]),
            ("add_promo_code", [{}]),
            ("save_publishing_account", [{}]),
            ("save_software_user", [{}]),
            ("save_settings", [{}]),
            ("create_managed_device_config", [{}]),
            ("publish_update", ["release.zip", "9.9.9", "Denied"]),
        ):
            with self.subTest(method=method):
                denied = self.bridge.desktop_rpc(method, args)
                self.assertFalse(denied["ok"], denied)
        self.assertEqual(self.api._catalog.list_platforms().get("items"), [])

        bootstrap = self.bridge.desktop_rpc("get_bootstrap", [])
        self.assertTrue(bootstrap["ok"], bootstrap)
        employee_settings = bootstrap["data"]["settings"]
        self.assertNotIn("text_endpoint", employee_settings["providers"])
        self.assertNotIn("text_model", employee_settings["providers"])
        self.assertNotIn("tts_api_key", employee_settings["providers"])
        self.assertNotIn("endpoint", employee_settings["hub"])
        self.assertNotIn("mode", employee_settings["hub"])
        runtime = self.bridge.desktop_rpc("get_local_runtime_snapshot", [])
        self.assertTrue(runtime["ok"], runtime)
        self.assertIn("embedded_kokoro_ready", runtime["data"]["system"])
        self_check = self.bridge.desktop_rpc("get_local_self_check", [])
        self.assertTrue(self_check["ok"], self_check)
        self.assertIn("checks", self_check["data"])
        self.assertIn(
            "ffmpeg", {item["key"] for item in self_check["data"]["checks"]}
        )
        library = self.bridge.desktop_rpc("get_library_bootstrap", [])
        self.assertTrue(library["ok"], library)

    def test_employee_library_bootstrap_is_an_explicit_desktop_contract(self) -> None:
        logged_in = self.bridge.desktop_login("employee-one", "xs123456")
        self.assertTrue(logged_in["ok"], logged_in)

        library = self.bridge.get_library_bootstrap()

        self.assertTrue(library["ok"], library)
        self.assertIn("get_library_bootstrap", EMPLOYEE_DESKTOP_METHODS)
        self.assertEqual(
            DESKTOP_RPC_PERMISSIONS["get_library_bootstrap"],
            WEB_RPC_PERMISSIONS["get_library_bootstrap"],
        )

    def test_employee_can_read_style_catalog_but_cannot_overwrite_team_defaults(self) -> None:
        logged_in = self.bridge.desktop_login("employee-one", "xs123456")
        self.assertTrue(logged_in["ok"], logged_in)

        styles = self.bridge.desktop_rpc("get_visual_style_presets", [])
        self.assertTrue(styles["ok"], styles)
        self.assertIn("intro_card", styles["data"])
        self.assertIn("subtitle", styles["data"])

        denied = self.bridge.desktop_rpc(
            "save_settings",
            [{"subtitle_preset": "bold_drama"}],
        )
        self.assertFalse(denied["ok"], denied)
        self.assertNotEqual(
            self.api._state.settings.subtitle_preset,
            "bold_drama",
        )

    def test_employee_can_update_only_the_installed_workstation(self) -> None:
        logged_in = self.bridge.desktop_login("employee-one", "xs123456")
        self.assertTrue(logged_in["ok"], logged_in)
        update_status = {
            "state": "downloaded",
            "current_version": "0.4.0-rc4",
            "available_version": "0.4.1",
            "downloaded": True,
            "rendering_busy": False,
        }
        for method, target in (
            ("check_for_updates", "check_now"),
            ("download_update", "download"),
            ("schedule_update_on_restart", "schedule_on_restart"),
            ("cancel_scheduled_update", "cancel_schedule"),
        ):
            with self.subTest(method=method), patch.object(
                self.api._update_manager, target, return_value=dict(update_status)
            ):
                result = self.bridge.desktop_rpc(method, [])
                self.assertTrue(result["ok"], result)

        destroyed = threading.Event()

        class FakeWindow:
            def destroy(self) -> None:
                destroyed.set()

        self.api._attach_window(FakeWindow())
        with patch.object(
            self.api._update_manager,
            "schedule_on_restart",
            return_value=dict(update_status),
        ), patch("storyforge.api.time.sleep", return_value=None):
            restarted = self.bridge.desktop_rpc("restart_to_apply_update", [])
            self.assertTrue(restarted["ok"], restarted)
            self.assertTrue(restarted["data"]["exit_queued"])
            self.assertTrue(destroyed.wait(1.0))

    def test_effective_permission_overrides_apply_in_desktop_shell(self) -> None:
        self.api._catalog.set_user_permission(
            self.employee_id, "voice.preview", False
        )
        self.api._catalog.set_user_permission(
            self.employee_id, "library.edit", True
        )
        logged_in = self.bridge.desktop_login("employee-one", "xs123456")
        self.assertTrue(logged_in["ok"], logged_in)

        denied = self.bridge.desktop_rpc(
            "generate_voice_candidates", ["missing", "suspense"]
        )
        self.assertFalse(denied["ok"], denied)
        self.assertIn("权限", denied["error"])

        imported = self.bridge.desktop_rpc(
            "import_novel_text",
            [
                {
                    "title": "Permission Override Story",
                    "text": "The phone rang at midnight. She finally answered it.",
                    "language": "en",
                }
            ],
        )
        self.assertTrue(imported["ok"], imported)

    def test_employee_can_switch_only_free_local_tts_provider(self) -> None:
        logged_in = self.bridge.desktop_login("employee-one", "xs123456")
        self.assertTrue(logged_in["ok"], logged_in)
        switched = self.bridge.desktop_rpc("set_local_tts_provider", ["edge_tts"])
        self.assertTrue(switched["ok"], switched)
        self.assertEqual(switched["data"]["tts_provider"], "edge_tts")
        denied = self.bridge.desktop_rpc("set_local_tts_provider", ["deepgram"])
        self.assertFalse(denied["ok"], denied)

    def test_employee_desktop_can_manage_only_its_personal_production_presets(self) -> None:
        admin_personal = self.api._catalog.save_production_preset(
            {
                "name": "团队通用",
                "recipe": {"production_settings": {"output_fps": 60}},
            },
            actor_user_id=self.api._catalog._web_user_by_username(
                "storyforge-owner"
            )["id"],
        )
        logged_in = self.bridge.desktop_login("employee-one", "xs123456")
        self.assertTrue(logged_in["ok"], logged_in)

        listing = self.bridge.desktop_rpc("get_production_presets", [])
        self.assertTrue(listing["ok"], listing)
        self.assertNotIn(
            admin_personal["id"],
            {item["id"] for item in listing["data"]["items"]},
        )

        created = self.bridge.desktop_rpc(
            "save_production_preset",
            [
                {
                    "name": "我的桌面方案",
                    "recipe": {"production_settings": {"output_fps": 60}},
                }
            ],
        )
        self.assertTrue(created["ok"], created)
        self.assertEqual(created["data"]["owner_user_id"], self.employee_id)
        self.assertTrue(created["data"]["editable"])
        deleted = self.bridge.desktop_rpc(
            "delete_production_preset", [created["data"]["id"]]
        )
        self.assertTrue(deleted["ok"], deleted)

    def test_admin_is_allowed_but_private_api_is_never_exposed(self) -> None:
        logged_in = self.bridge.desktop_login("storyforge-owner", "xs123456")
        self.assertTrue(logged_in["ok"], logged_in)
        saved = self.bridge.desktop_rpc("save_platform", [{"name": "Allowed Platform"}])
        self.assertTrue(saved["ok"], saved)
        for method, args in (
            ("_shutdown", []),
            ("list_hub_user_tokens", [self.employee_id]),
            ("issue_hub_user_token", [self.employee_id, "Legacy device"]),
            ("revoke_hub_user_token", ["legacy-token-id"]),
        ):
            with self.subTest(method=method):
                denied = self.bridge.desktop_rpc(method, args)
                self.assertFalse(denied["ok"], denied)

        main_source = (Path(__file__).parents[1] / "storyforge" / "main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("js_api=StoryForgeDesktopBridge(api)", main_source)
        self.assertNotIn("js_api=api,", main_source)


if __name__ == "__main__":
    unittest.main()
