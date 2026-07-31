from __future__ import annotations

import tempfile
import unittest
import socket
from pathlib import Path

from storyforge.api import StoryForgeApi
from storyforge.catalog import CatalogConflictError, CatalogRepository
from storyforge.config import SettingsRepository
from storyforge.credentials import (
    DEFAULT_EMPLOYEE_PASSWORD,
    password_matches,
    validate_new_password,
)
from storyforge.models import AppSettings


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class PasswordContractTests(unittest.TestCase):
    def test_password_is_exactly_eight_visible_ascii_characters(self) -> None:
        for value in ("xs123456", "Ab1!cD2?", "!!!!!!!!"):
            self.assertEqual(validate_new_password(value), value)
        for value in (
            "short7!",
            "toolong9!",
            "has gap!",
            "中文123456",
            "line\n12!",
        ):
            with self.assertRaises(ValueError, msg=value):
                validate_new_password(value)


class AccountAndDeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.catalog = CatalogRepository(self.root / "catalog.sqlite3")
        self.owner = self.catalog.save_user(
            {"username": "owner", "role": "admin", "active": True}
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_employee_defaults_cover_production_but_not_team_management(self) -> None:
        employee = self.catalog.save_user(
            {"username": "employee", "role": "producer", "active": True}
        )
        effective = self.catalog.get_effective_permissions(employee["id"])[
            "effective"
        ]
        for permission in (
            "drafts.create",
            "jobs.retry_own",
            "production.execute",
            "voice.preview",
            "text.assist",
            "presets.manage_own",
            "updates.manage_own",
        ):
            self.assertTrue(effective[permission], permission)
        for permission in (
            "library.edit",
            "platforms.manage",
            "promo_codes.manage",
            "users.manage",
            "hub.manage",
        ):
            self.assertFalse(effective[permission], permission)

    def test_new_device_is_flagged_until_an_admin_reviews_it(self) -> None:
        employee = self.catalog.save_user(
            {"username": "employee", "role": "producer", "active": True}
        )
        created = self.catalog.register_hub_device(
            {
                "installation_id_hash": "a" * 64,
                "name": "Editing PC",
                "last_user_id": employee["id"],
                "metadata": {"needs_admin_review": False},
            },
            actor_user_id=employee["id"],
        )
        device = created["device"]
        self.assertTrue(device["needs_admin_review"])
        self.assertTrue(device["first_login_at"])
        self.assertFalse(device["admin_reviewed_at"])
        reviewed = self.catalog.acknowledge_hub_device(
            device["id"], actor_user_id=self.owner["id"]
        )
        self.assertFalse(reviewed["needs_admin_review"])
        self.assertTrue(reviewed["admin_reviewed_at"])

    def test_deleting_employee_revokes_login_but_preserves_device_history(self) -> None:
        employee = self.catalog.save_user(
            {"username": "employee", "role": "producer", "active": True}
        )
        device = self.catalog.register_hub_device(
            {
                "installation_id_hash": "b" * 64,
                "name": "Render PC",
                "last_user_id": employee["id"],
            },
            actor_user_id=employee["id"],
        )["device"]
        self.catalog.issue_hub_access_token(
            employee["id"], label="Render PC", device_id=device["id"]
        )
        deleted = self.catalog.delete_user(
            employee["id"], actor_user_id=self.owner["id"]
        )
        self.assertTrue(deleted["deleted"])
        self.assertEqual(self.catalog.list_hub_access_tokens()["total"], 0)
        self.assertEqual(
            self.catalog.get_hub_device(device["id"])["last_user_id"], ""
        )
        actions = {
            event["action"] for event in self.catalog.list_audit_events()["items"]
        }
        self.assertIn("user.deleted", actions)
        with self.assertRaises(CatalogConflictError):
            self.catalog.delete_user(
                self.owner["id"], actor_user_id=self.owner["id"]
            )


class DesktopSessionTests(unittest.TestCase):
    def test_default_employee_password_and_thirty_day_desktop_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = SettingsRepository(Path(directory))
            api = StoryForgeApi(repository=repository)
            try:
                created = api.save_software_user(
                    {"username": "worker", "role": "producer"}
                )
                self.assertTrue(created["ok"], created)
                private = api._catalog._web_user_by_username("worker")
                self.assertTrue(
                    password_matches(
                        DEFAULT_EMPLOYEE_PASSWORD,
                        str(private["password_hash"]),
                    )
                )
                logged_in = api.desktop_login(
                    "worker", DEFAULT_EMPLOYEE_PASSWORD
                )
                self.assertTrue(logged_in["ok"], logged_in)
                self.assertEqual(logged_in["data"]["user"]["role"], "producer")
                self.assertIn("T", logged_in["data"]["expires_at"])
            finally:
                api._shutdown()

            restarted = StoryForgeApi(repository=repository)
            try:
                status = restarted.desktop_session_status()
                self.assertTrue(status["data"]["authenticated"])
                self.assertEqual(status["data"]["user"]["username"], "worker")
                blocked = restarted.save_settings(
                    {"hub": {"mode": "host", "listen_port": 8765}}
                )
                self.assertFalse(blocked["ok"])
                restarted.desktop_logout()
                self.assertFalse(
                    restarted.desktop_session_status()["data"]["authenticated"]
                )
            finally:
                restarted._shutdown()

    def test_client_desktop_password_login_is_revoked_when_employee_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host_repository = SettingsRepository(root / "host")
            host_settings = AppSettings()
            host_settings.hub.mode = "host"
            host_settings.hub.listen_host = "127.0.0.1"
            host_settings.hub.listen_port = _free_port()
            host_repository.save(host_settings, [], [])
            host = StoryForgeApi(repository=host_repository)
            client: StoryForgeApi | None = None
            try:
                employee = host.save_software_user(
                    {"username": "remote-worker", "role": "producer"}
                )["data"]
                client_repository = SettingsRepository(root / "client")
                client_repository.save(AppSettings(), [], [])
                client = StoryForgeApi(repository=client_repository)
                enrolled = client.connect_hub_with_password(
                    host._hub_server.base_url,  # type: ignore[union-attr]
                    "remote-worker",
                    DEFAULT_EMPLOYEE_PASSWORD,
                    "Remote render PC",
                )
                self.assertTrue(enrolled["ok"], enrolled)
                logged_in = client.desktop_login(
                    "remote-worker", DEFAULT_EMPLOYEE_PASSWORD
                )
                self.assertTrue(logged_in["ok"], logged_in)
                self.assertTrue(
                    logged_in["data"]["capabilities"]["client_local"]
                )
                deleted = host.delete_software_user(employee["id"])
                self.assertTrue(deleted["ok"], deleted)
                self.assertFalse(
                    client.desktop_session_status()["data"]["authenticated"]
                )
            finally:
                if client is not None:
                    client._shutdown()
                host._shutdown()


if __name__ == "__main__":
    unittest.main()
