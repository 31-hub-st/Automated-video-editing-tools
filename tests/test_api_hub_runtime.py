from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from storyforge.api import StoryForgeApi
from storyforge.config import SettingsRepository
from storyforge.hub import HubAuthenticationError, HubRemoteError
from storyforge.models import AppSettings, JobStatus, PlatformProfile, RenderJob
from storyforge.pipeline import PipelineRunner, job_workspace_directory
from storyforge.providers.text import TextRequest, TextResult


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ApiHubRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.host_api: StoryForgeApi | None = None
        self.client_api: StoryForgeApi | None = None
        self.second_client_api: StoryForgeApi | None = None

    def tearDown(self) -> None:
        if self.client_api is not None:
            self.client_api._shutdown()
        if self.second_client_api is not None:
            self.second_client_api._shutdown()
        if self.host_api is not None:
            self.host_api._shutdown()
        self.temporary.cleanup()

    def test_non_host_public_backup_health_cannot_claim_readiness(self) -> None:
        api = object.__new__(StoryForgeApi)
        api._runtime_hub_mode = "local"
        api._backup_manager = SimpleNamespace(
            health_status=lambda: {
                "enabled": True,
                "running": True,
                "state": "ready",
                "has_error": False,
                "ready": True,
                "operational": True,
            }
        )

        status = api._backup_health_status_value()

        self.assertFalse(status["available"])
        self.assertFalse(status["enabled"])
        self.assertFalse(status["running"])
        self.assertFalse(status["ready"])
        self.assertFalse(status["operational"])

    def test_employee_portable_host_settings_fail_before_hub_server_construction(
        self,
    ) -> None:
        repository = SettingsRepository(self.root / "portable-host")
        settings = AppSettings()
        settings.hub.mode = "host"
        repository.save(settings, [], [])

        with (
            patch.dict(
                os.environ,
                {
                    "STORYFORGE_DATA_DIR": str(repository.data_dir),
                    "STORYFORGE_PORTABLE_MODE": "1",
                    "STORYFORGE_FROZEN_HUB_DATA_ROOT": str(repository.data_dir),
                },
                clear=True,
            ),
            patch("storyforge.api.HubServer") as server,
            patch("storyforge.api.UpdateManager.start"),
            patch("storyforge.api.HubBackupManager.start_daily"),
            self.assertRaisesRegex(RuntimeError, "portable employee runtime"),
        ):
            StoryForgeApi(repository=repository)

        server.assert_not_called()

    def test_frozen_host_rejects_data_root_created_after_startup_authorization(
        self,
    ) -> None:
        repository = SettingsRepository(self.root / "unapproved-frozen-host")
        settings = AppSettings()
        settings.hub.mode = "host"
        repository.save(settings, [], [])

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.dict(
                os.environ,
                {
                    "STORYFORGE_DATA_DIR": str(repository.data_dir),
                    "STORYFORGE_PORTABLE_MODE": "",
                    "STORYFORGE_DEPLOYMENT_ROLE": "Hub",
                },
                clear=True,
            ),
            patch("storyforge.api.HubServer") as server,
            patch("storyforge.api.UpdateManager.start"),
            patch("storyforge.api.HubBackupManager.start_daily"),
            self.assertRaisesRegex(RuntimeError, "explicit STORYFORGE_DATA_DIR"),
        ):
            StoryForgeApi(repository=repository)

        server.assert_not_called()

    def test_frozen_host_allows_preconfigured_authorized_data_root(self) -> None:
        repository = SettingsRepository(self.root / "approved-frozen-host")
        settings = AppSettings()
        settings.hub.mode = "host"
        repository.save(settings, [], [])
        authorized = str(repository.data_dir.resolve(strict=False))

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.dict(
                os.environ,
                {
                    "STORYFORGE_DATA_DIR": authorized,
                    "STORYFORGE_PORTABLE_MODE": "",
                    "STORYFORGE_FROZEN_HUB_DATA_ROOT": authorized,
                    "STORYFORGE_DEPLOYMENT_ROLE": "Hub",
                },
                clear=True,
            ),
            patch("storyforge.api.HubServer") as server,
        ):
            server.return_value.start.return_value = server.return_value
            self.host_api = StoryForgeApi(repository=repository)

        server.assert_called_once()

    def test_hub_only_shutdown_never_launches_a_pending_client_update(self) -> None:
        repository = SettingsRepository(self.root / "hub-only-shutdown")
        settings = AppSettings()
        settings.hub.mode = "host"
        settings.hub.listen_host = "127.0.0.1"
        settings.hub.listen_port = _free_port()
        repository.save(settings, [], [])
        self.host_api = StoryForgeApi(
            repository=repository,
            local_production_enabled=False,
        )
        self.host_api._update_manager._pending_ready = True

        with patch.object(
            self.host_api._update_manager,
            "launch_scheduled_update",
        ) as launch_update:
            self.host_api._shutdown()
        self.host_api = None

        launch_update.assert_not_called()

    def test_local_shutdown_keeps_the_existing_update_handoff(self) -> None:
        repository = SettingsRepository(self.root / "local-shutdown")
        repository.save(AppSettings(), [], [])
        api = StoryForgeApi(repository=repository)
        api._update_manager._pending_ready = True

        with patch.object(
            api._update_manager,
            "launch_scheduled_update",
        ) as launch_update:
            api._shutdown()

        launch_update.assert_called_once_with()

    def test_stale_activation_failure_cannot_disconnect_newer_hub_client(self) -> None:
        repository = SettingsRepository(self.root / "activation-race-client")
        repository.save(AppSettings(), [], [])
        self.client_api = StoryForgeApi(repository=repository)

        newer_client = object()

        class RejectedStaleClient:
            authentication_failure_callback = None

            @staticmethod
            def verify_identity() -> dict:
                raise HubAuthenticationError(
                    401,
                    "device_session_revoked",
                    "the stale workstation token was revoked",
                )

        stale_client = RejectedStaleClient()
        with self.client_api._hub_client_lock:
            self.client_api._hub_client = newer_client  # type: ignore[assignment]

        with self.assertRaises(HubAuthenticationError):
            self.client_api._activate_hub_client(stale_client)  # type: ignore[arg-type]

        self.assertIs(self.client_api._hub_client_snapshot(), newer_client)

    def test_stale_device_sync_exception_cannot_disconnect_reenrolled_client(
        self,
    ) -> None:
        repository = SettingsRepository(self.root / "sync-race-client")
        repository.save(AppSettings(), [], [])
        self.client_api = StoryForgeApi(repository=repository)
        stale_client = object()
        newer_client = object()
        rejection = HubAuthenticationError(
            401,
            "unauthorized",
            "the stale workstation token was revoked",
        )
        with self.client_api._hub_client_lock:
            self.client_api._hub_client = newer_client  # type: ignore[assignment]

        def stale_sync_failure() -> None:
            # HubClient's identity-aware callback has already rejected this
            # stale source without touching the newly enrolled transport.
            self.assertFalse(
                self.client_api._mark_hub_authentication_failed(  # type: ignore[union-attr]
                    stale_client,  # type: ignore[arg-type]
                    rejection,
                )
            )
            self.client_api._device_sync_stop.set()  # type: ignore[union-attr]
            raise rejection

        with patch.object(
            self.client_api,
            "_device_sync_once",
            side_effect=stale_sync_failure,
        ):
            self.client_api._device_sync_loop()

        self.assertIs(self.client_api._hub_client_snapshot(), newer_client)
        self.assertEqual(self.client_api._hub_error, "")
        self.assertNotEqual(
            self.client_api._device_sync_status_value()["state"],
            "authentication_required",
        )

    def test_stale_activation_cannot_publish_after_newer_token_is_installed(
        self,
    ) -> None:
        repository = SettingsRepository(self.root / "late-activation-client")
        repository.save(AppSettings(), [], [])
        self.client_api = StoryForgeApi(repository=repository)
        old_verified = threading.Event()
        release_old = threading.Event()

        class Client:
            authentication_failure_callback = None

            def __init__(self, token: str) -> None:
                self.token = token

            def verify_identity(self) -> dict:
                if self.token == "old-token":
                    old_verified.set()
                    if not release_old.wait(5):
                        raise TimeoutError("test did not release stale activation")
                return {"user": {"id": self.token}}

        class Proxy:
            def __init__(self, client: Client) -> None:
                self.client = client

            @staticmethod
            def list_platforms() -> dict:
                return {"items": []}

        stale_client = Client("old-token")
        newer_client = Client("new-token")
        self.client_api._state.update_settings(
            {"hub": {"mode": "client", "access_token": stale_client.token}}
        )

        with (
            patch("storyforge.api.HubCatalogProxy", side_effect=Proxy),
            patch("storyforge.api.LibraryService", side_effect=lambda catalog, *_a, **_k: catalog),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            stale_activation = executor.submit(
                self.client_api._activate_hub_client,
                stale_client,
            )
            self.assertTrue(old_verified.wait(5), "stale activation did not start")
            self.client_api._state.update_settings(
                {"hub": {"access_token": newer_client.token}}
            )
            self.client_api._activate_hub_client(newer_client)
            release_old.set()
            with self.assertRaisesRegex(RuntimeError, "superseded"):
                stale_activation.result(timeout=5)

        self.assertIs(self.client_api._hub_client_snapshot(), newer_client)
        self.assertIs(self.client_api._catalog.client, newer_client)

    def test_hub_status_prefers_real_lan_address_over_virtual_adapter(self) -> None:
        values = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.18.0.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.225", 0)),
        ]
        with patch("storyforge.api.socket.getaddrinfo", return_value=values):
            self.assertEqual(StoryForgeApi._local_ipv4(), "10.0.0.225")

    def test_hub_status_ignores_benchmark_adapter_without_a_lan_address(self) -> None:
        values = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.18.0.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
        ]
        with patch("storyforge.api.socket.getaddrinfo", return_value=values):
            self.assertEqual(StoryForgeApi._local_ipv4(), "127.0.0.1")

    def test_shared_cover_and_logo_cache_is_verified_and_repairs_itself(self) -> None:
        payloads = {
            "covers/novel.jpg": b"complete-cover-image",
            "platform-assets/goodnovel.png": b"complete-platform-logo",
        }

        class DownloadingClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []
                self.metadata_calls: list[tuple[str, str]] = []

            def file_metadata(self, root_alias, relative):
                self.metadata_calls.append((root_alias, relative))
                payload = payloads[relative]
                return {
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "etag": f'"sha256-{hashlib.sha256(payload).hexdigest()}"',
                }

            def download_file(self, root_alias, relative, *, destination):
                self.calls.append((root_alias, relative))
                payload = payloads[relative]
                destination_path = Path(destination)
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                destination_path.write_bytes(payload)
                return {
                    "path": str(destination_path),
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "content_type": "application/octet-stream",
                }

        client = DownloadingClient()
        api = object.__new__(StoryForgeApi)
        api._repository = SimpleNamespace(data_dir=self.root / "cache-client")
        api._runtime_hub_mode = "client"
        api._hub_client = client
        cover_ref = "hub://attachments/covers/novel.jpg"
        logo_ref = "hub://attachments/platform-assets/goodnovel.png"

        cover_path = Path(api._resolve_shared_file(cover_ref, group="covers"))
        logo_path = Path(
            api._resolve_shared_file(logo_ref, group="platform-assets")
        )
        self.assertEqual(cover_path.read_bytes(), payloads["covers/novel.jpg"])
        self.assertEqual(
            logo_path.read_bytes(), payloads["platform-assets/goodnovel.png"]
        )
        self.assertEqual(len(client.calls), 2)

        # Valid cache entries are reused without another network transfer.
        api._resolve_shared_file(cover_ref, group="covers")
        api._resolve_shared_file(logo_ref, group="platform-assets")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(client.metadata_calls), 4)

        # Replacing a Hub asset at the same path, even with the same size,
        # invalidates the sidecar and downloads only that changed body.
        payloads["covers/novel.jpg"] = b"R" * len(payloads["covers/novel.jpg"])
        api._resolve_shared_file(cover_ref, group="covers")
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(cover_path.read_bytes(), payloads["covers/novel.jpg"])

        # Same-sized corruption and a legacy zero-byte cache are both repaired.
        cover_path.write_bytes(b"x" * len(payloads["covers/novel.jpg"]))
        logo_path.write_bytes(b"")
        api._resolve_shared_file(cover_ref, group="covers")
        api._resolve_shared_file(logo_ref, group="platform-assets")
        self.assertEqual(len(client.calls), 5)
        self.assertEqual(cover_path.read_bytes(), payloads["covers/novel.jpg"])
        self.assertEqual(
            logo_path.read_bytes(), payloads["platform-assets/goodnovel.png"]
        )

    def test_host_backup_lifecycle_and_admin_api(self) -> None:
        port = _free_port()
        repository = SettingsRepository(self.root / "backup-host")
        settings = AppSettings()
        settings.hub.mode = "host"
        settings.hub.listen_host = "127.0.0.1"
        settings.hub.listen_port = port
        repository.save(settings, [], [])
        self.host_api = StoryForgeApi(repository=repository)

        bootstrap = self.host_api.get_bootstrap()
        self.assertTrue(bootstrap["ok"], bootstrap)
        backup_status = bootstrap["data"]["backup_status"]
        self.assertTrue(backup_status["available"])
        self.assertTrue(backup_status["enabled"])
        self.assertTrue(backup_status["running"])
        self.assertEqual(
            bootstrap["data"]["hub_status"]["backup"]["available"], True
        )

        created = self.host_api.create_hub_backup()
        self.assertTrue(created["ok"], created)
        snapshot = created["data"]["snapshot"]
        self.assertEqual(snapshot["reason"], "manual")
        self.assertTrue(snapshot["filename"].endswith(".sfbak"))
        self.assertNotIn("path", snapshot)

        listed = self.host_api.list_hub_backups()
        self.assertTrue(listed["ok"], listed)
        self.assertGreaterEqual(listed["data"]["total"], 1)
        self.assertTrue(
            any(item["id"] == snapshot["id"] for item in listed["data"]["items"])
        )
        self.assertTrue(
            all("path" not in item for item in listed["data"]["items"])
        )

        manager = self.host_api._backup_manager
        self.host_api._shutdown()
        self.host_api = None
        stopped = manager.status()
        self.assertFalse(stopped["enabled"])
        self.assertFalse(stopped["running"])
        self.assertEqual(stopped["state"], "stopped")

    def test_password_connect_persists_only_protected_token_and_revocation_disconnects(self) -> None:
        port = _free_port()
        host_repository = SettingsRepository(self.root / "password-host")
        host_settings = AppSettings()
        host_settings.hub.mode = "host"
        host_settings.hub.listen_host = "127.0.0.1"
        host_settings.hub.listen_port = port
        host_repository.save(host_settings, [], [])
        self.host_api = StoryForgeApi(repository=host_repository)
        member = self.host_api.save_software_user(
            {
                "username": "renderer-password",
                "display_name": "Renderer Password",
                "role": "producer",
                "active": True,
                "initial_password": "Rp1!2026",
            }
        )
        self.assertTrue(member["ok"], member)

        client_repository = SettingsRepository(self.root / "password-client")
        client_repository.save(AppSettings(), [], [])
        self.client_api = StoryForgeApi(repository=client_repository)
        connected = self.client_api.connect_hub_with_password(
            self.host_api._hub_server.base_url,  # type: ignore[union-attr]
            "renderer-password",
            "Rp1!2026",
            "Password Client PC",
        )
        self.assertTrue(connected["ok"], connected)
        self.assertTrue(connected["data"]["connected"])
        self.assertNotIn("token", connected["data"])
        self.assertNotIn("password", connected["data"])

        loaded, _platforms, _batches = client_repository.load()
        self.assertEqual(loaded.hub.mode, "client")
        self.assertEqual(loaded.hub.account_username, "renderer-password")
        self.assertTrue(loaded.hub.access_token.startswith("sfh_"))
        raw_settings = client_repository.settings_path.read_text(encoding="utf-8")
        self.assertNotIn("Rp1!2026", raw_settings)
        self.assertNotIn(loaded.hub.access_token, raw_settings)

        tokens = self.host_api.list_hub_user_tokens(member["data"]["id"])
        self.assertEqual(tokens["data"]["total"], 1)
        token_id = tokens["data"]["items"][0]["id"]
        revoked = self.host_api.revoke_hub_user_token(token_id)
        self.assertTrue(revoked["ok"], revoked)
        status = self.client_api.get_hub_status()
        self.assertFalse(status["data"]["connected"])
        self.assertEqual(status["data"]["status"], "offline")

    def test_desktop_login_enrolls_unbound_client_and_worker_setup_is_nonblocking(self) -> None:
        port = _free_port()
        host_repository = SettingsRepository(self.root / "login-enroll-host")
        host_settings = AppSettings()
        host_settings.hub.mode = "host"
        host_settings.hub.listen_host = "127.0.0.1"
        host_settings.hub.listen_port = port
        host_repository.save(host_settings, [], [])
        self.host_api = StoryForgeApi(repository=host_repository)
        member = self.host_api.save_software_user(
            {
                "username": "login-renderer",
                "display_name": "Login Renderer",
                "role": "producer",
                "active": True,
                "initial_password": "Lr1!2026",
            }
        )["data"]

        client_repository = SettingsRepository(self.root / "login-enroll-client")
        client_settings = AppSettings()
        client_settings.hub.mode = "client"
        client_settings.hub.endpoint = self.host_api._hub_server.base_url  # type: ignore[union-attr]
        client_settings.hub.access_token = ""
        client_settings.hub.device_id = ""
        client_settings.hub.device_name = ""
        client_repository.save(client_settings, [], [])
        self.client_api = StoryForgeApi(repository=client_repository)
        self.assertIsNone(self.client_api._hub_client)

        with patch(
            "storyforge.worker.ensure_local_worker_autostart",
            side_effect=RuntimeError("synthetic task scheduler failure"),
        ):
            logged_in = self.client_api.desktop_login(
                "login-renderer", "Lr1!2026"
            )

        self.assertTrue(logged_in["ok"], logged_in)
        self.assertEqual(logged_in["data"]["user"]["id"], member["id"])
        self.assertEqual(
            logged_in["data"]["worker_autostart"]["state"], "warning"
        )
        self.assertEqual(
            logged_in["data"]["notices"][0]["code"],
            "worker_autostart_failed",
        )
        loaded, _platforms, _batches = client_repository.load()
        self.assertEqual(loaded.hub.mode, "client")
        self.assertEqual(loaded.hub.account_username, "login-renderer")
        self.assertEqual(loaded.hub.device_name, socket.gethostname())
        self.assertTrue(loaded.hub.device_id)
        self.assertTrue(loaded.hub.access_token.startswith("sfh_"))
        raw = client_repository.settings_path.read_text(encoding="utf-8")
        self.assertNotIn("Lr1!2026", raw)
        self.assertNotIn(loaded.hub.access_token, raw)

        events: list[str] = []
        self.client_api._client_web_server = None
        self.client_api._desktop_ui_root = self.root

        def start_local_worker(*_args: object, **_kwargs: object) -> object:
            events.append("desktop-worker")
            server = SimpleNamespace(stop=lambda: None)
            self.client_api._client_web_server = server
            return server

        def install_login_task() -> dict[str, object]:
            events.append("login-task")
            return {"state": "enabled", "automatic": True}

        with (
            patch("storyforge.api.sys.frozen", True, create=True),
            patch.object(
                self.client_api,
                "_ensure_local_worker_server",
                side_effect=start_local_worker,
            ),
            patch(
                "storyforge.worker.ensure_local_worker_autostart",
                side_effect=install_login_task,
            ),
        ):
            setup = self.client_api._worker_autostart_after_login()
        self.assertEqual(setup["state"], "enabled")
        self.assertEqual(events, ["desktop-worker", "login-task"])

    def test_desktop_login_switches_account_on_same_installation_and_rotates_token(self) -> None:
        port = _free_port()
        host_repository = SettingsRepository(self.root / "account-switch-host")
        host_settings = AppSettings()
        host_settings.hub.mode = "host"
        host_settings.hub.listen_host = "127.0.0.1"
        host_settings.hub.listen_port = port
        host_repository.save(host_settings, [], [])
        self.host_api = StoryForgeApi(repository=host_repository)
        first_member = self.host_api.save_software_user(
            {
                "username": "first-renderer",
                "role": "producer",
                "active": True,
                "initial_password": "Fr1!2026",
            }
        )["data"]
        second_member = self.host_api.save_software_user(
            {
                "username": "second-renderer",
                "role": "producer",
                "active": True,
                "initial_password": "Sr1!2026",
            }
        )["data"]

        client_repository = SettingsRepository(self.root / "account-switch-client")
        client_settings = AppSettings()
        client_settings.hub.mode = "client"
        client_settings.hub.endpoint = self.host_api._hub_server.base_url  # type: ignore[union-attr]
        installation_id = client_settings.hub.installation_id
        client_repository.save(client_settings, [], [])
        self.client_api = StoryForgeApi(repository=client_repository)

        with patch(
            "storyforge.worker.ensure_local_worker_autostart",
            return_value={"state": "development", "automatic": False},
        ):
            first_login = self.client_api.desktop_login(
                "first-renderer", "Fr1!2026"
            )
        self.assertTrue(first_login["ok"], first_login)
        first_settings, _platforms, _batches = client_repository.load()
        first_token = first_settings.hub.access_token
        device_id = first_settings.hub.device_id
        self.assertEqual(first_login["data"]["user"]["id"], first_member["id"])

        # A failed verification is retried through password enrollment so a
        # stale local account label can self-heal.  Invalid credentials must
        # still fail before either the stable device binding or its live token
        # is changed.
        rejected = self.client_api.desktop_login(
            "first-renderer", "DefinitelyWrong1!"
        )
        self.assertFalse(rejected["ok"], rejected)
        after_rejection, _platforms, _batches = client_repository.load()
        self.assertEqual(after_rejection.hub.device_id, device_id)
        self.assertEqual(after_rejection.hub.account_username, "first-renderer")
        self.assertEqual(after_rejection.hub.access_token, first_token)
        rejected_identity = self.host_api._catalog.resolve_hub_access_identity(
            first_token
        )
        self.assertTrue(rejected_identity["authenticated"])
        self.assertEqual(rejected_identity["user_id"], first_member["id"])
        rejected_device = self.host_api._catalog.get_hub_device(device_id)
        self.assertEqual(rejected_device["last_user_id"], first_member["id"])

        with patch(
            "storyforge.worker.ensure_local_worker_autostart",
            return_value={"state": "development", "automatic": False},
        ):
            second_login = self.client_api.desktop_login(
                "second-renderer", "Sr1!2026"
            )

        self.assertTrue(second_login["ok"], second_login)
        self.assertEqual(second_login["data"]["user"]["id"], second_member["id"])
        switched, _platforms, _batches = client_repository.load()
        self.assertEqual(switched.hub.installation_id, installation_id)
        self.assertEqual(switched.hub.device_id, device_id)
        self.assertEqual(switched.hub.account_username, "second-renderer")
        self.assertNotEqual(switched.hub.access_token, first_token)
        self.assertEqual(
            self.host_api._catalog.resolve_hub_access_identity(first_token),
            {"authenticated": False},
        )
        active_tokens = self.host_api._catalog.list_hub_access_tokens(
            include_revoked=False
        )["items"]
        self.assertEqual(len(active_tokens), 1)
        self.assertEqual(active_tokens[0]["user_id"], second_member["id"])
        raw = client_repository.settings_path.read_text(encoding="utf-8")
        self.assertNotIn("Fr1!2026", raw)
        self.assertNotIn("Sr1!2026", raw)
        self.assertNotIn(switched.hub.access_token, raw)

    def test_account_switch_recovers_without_restart_after_activation_failure(self) -> None:
        port = _free_port()
        host_repository = SettingsRepository(self.root / "activation-recovery-host")
        host_settings = AppSettings()
        host_settings.hub.mode = "host"
        host_settings.hub.listen_host = "127.0.0.1"
        host_settings.hub.listen_port = port
        host_repository.save(host_settings, [], [])
        self.host_api = StoryForgeApi(repository=host_repository)
        first_member = self.host_api.save_software_user(
            {
                "username": "activation-first",
                "role": "producer",
                "active": True,
                "initial_password": "Af1!2026",
            }
        )["data"]
        second_member = self.host_api.save_software_user(
            {
                "username": "activation-second",
                "role": "producer",
                "active": True,
                "initial_password": "As1!2026",
            }
        )["data"]

        client_repository = SettingsRepository(
            self.root / "activation-recovery-client"
        )
        client_settings = AppSettings()
        client_settings.hub.mode = "client"
        client_settings.hub.endpoint = self.host_api._hub_server.base_url  # type: ignore[union-attr]
        installation_id = client_settings.hub.installation_id
        client_repository.save(client_settings, [], [])
        self.client_api = StoryForgeApi(repository=client_repository)

        with patch(
            "storyforge.worker.ensure_local_worker_autostart",
            return_value={"state": "development", "automatic": False},
        ):
            first_login = self.client_api.desktop_login(
                "activation-first", "Af1!2026"
            )
        self.assertTrue(first_login["ok"], first_login)
        first_settings, _platforms, _batches = client_repository.load()
        first_token = first_settings.hub.access_token
        device_id = first_settings.hub.device_id
        self.assertEqual(first_login["data"]["user"]["id"], first_member["id"])

        with patch.object(
            self.client_api,
            "_activate_hub_client",
            side_effect=RuntimeError("synthetic post-enrollment outage"),
        ):
            interrupted = self.client_api.desktop_login(
                "activation-second", "As1!2026"
            )
        self.assertFalse(interrupted["ok"], interrupted)
        self.assertIsNone(self.client_api._hub_client)
        interrupted_settings, _platforms, _batches = client_repository.load()
        interrupted_token = interrupted_settings.hub.access_token
        self.assertEqual(interrupted_settings.hub.installation_id, installation_id)
        self.assertEqual(interrupted_settings.hub.device_id, device_id)
        self.assertEqual(
            interrupted_settings.hub.account_username, "activation-second"
        )
        self.assertNotEqual(interrupted_token, first_token)
        self.assertEqual(
            self.host_api._catalog.resolve_hub_access_identity(first_token),
            {"authenticated": False},
        )

        # The same API process retries enrollment because the stale in-memory
        # transport was cleared.  No application restart is required.
        with patch(
            "storyforge.worker.ensure_local_worker_autostart",
            return_value={"state": "development", "automatic": False},
        ):
            recovered = self.client_api.desktop_login(
                "activation-second", "As1!2026"
            )
        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["data"]["user"]["id"], second_member["id"])
        recovered_settings, _platforms, _batches = client_repository.load()
        self.assertEqual(recovered_settings.hub.installation_id, installation_id)
        self.assertEqual(recovered_settings.hub.device_id, device_id)
        self.assertNotEqual(recovered_settings.hub.access_token, interrupted_token)
        self.assertEqual(
            self.host_api._catalog.resolve_hub_access_identity(interrupted_token),
            {"authenticated": False},
        )
        active_tokens = self.host_api._catalog.list_hub_access_tokens(
            include_revoked=False
        )["items"]
        self.assertEqual(len(active_tokens), 1)
        self.assertEqual(active_tokens[0]["user_id"], second_member["id"])

    def test_login_repairs_stale_local_account_label_against_bound_token(self) -> None:
        port = _free_port()
        host_repository = SettingsRepository(self.root / "stale-account-host")
        host_settings = AppSettings()
        host_settings.hub.mode = "host"
        host_settings.hub.listen_host = "127.0.0.1"
        host_settings.hub.listen_port = port
        host_repository.save(host_settings, [], [])
        self.host_api = StoryForgeApi(repository=host_repository)
        first_member = self.host_api.save_software_user(
            {
                "username": "bound-first",
                "role": "producer",
                "active": True,
                "initial_password": "Bf1!2026",
            }
        )["data"]
        second_member = self.host_api.save_software_user(
            {
                "username": "label-second",
                "role": "producer",
                "active": True,
                "initial_password": "Ls1!2026",
            }
        )["data"]

        client_repository = SettingsRepository(self.root / "stale-account-client")
        client_settings = AppSettings()
        client_settings.hub.mode = "client"
        client_settings.hub.endpoint = self.host_api._hub_server.base_url  # type: ignore[union-attr]
        client_repository.save(client_settings, [], [])
        self.client_api = StoryForgeApi(repository=client_repository)

        with patch(
            "storyforge.worker.ensure_local_worker_autostart",
            return_value={"state": "development", "automatic": False},
        ):
            first_login = self.client_api.desktop_login(
                "bound-first", "Bf1!2026"
            )
        self.assertTrue(first_login["ok"], first_login)
        original, _platforms, _batches = client_repository.load()
        original_token = original.hub.access_token
        device_id = original.hub.device_id
        self.assertEqual(first_login["data"]["user"]["id"], first_member["id"])

        # Simulate a legacy/partially-written settings file: the local label
        # says B while the bearer token remains authoritatively bound to A.
        self.client_api._state.update_settings(
            {"hub": {"account_username": "label-second"}}
        )
        stale, _platforms, _batches = client_repository.load()
        self.assertEqual(stale.hub.account_username, "label-second")
        self.assertEqual(stale.hub.access_token, original_token)

        with patch(
            "storyforge.worker.ensure_local_worker_autostart",
            return_value={"state": "development", "automatic": False},
        ):
            repaired = self.client_api.desktop_login(
                "label-second", "Ls1!2026"
            )
        self.assertTrue(repaired["ok"], repaired)
        self.assertEqual(repaired["data"]["user"]["id"], second_member["id"])
        fixed, _platforms, _batches = client_repository.load()
        self.assertEqual(fixed.hub.device_id, device_id)
        self.assertEqual(fixed.hub.account_username, "label-second")
        self.assertNotEqual(fixed.hub.access_token, original_token)
        self.assertEqual(
            self.host_api._catalog.resolve_hub_access_identity(original_token),
            {"authenticated": False},
        )
        fixed_identity = self.host_api._catalog.resolve_hub_access_identity(
            fixed.hub.access_token
        )
        self.assertTrue(fixed_identity["authenticated"])
        self.assertEqual(fixed_identity["user_id"], second_member["id"])
        self.assertEqual(fixed_identity["device_id"], device_id)

    def test_packaged_endpoint_turns_first_desktop_login_into_one_step_enrollment(self) -> None:
        port = _free_port()
        host_repository = SettingsRepository(self.root / "profile-login-host")
        host_settings = AppSettings()
        host_settings.hub.mode = "host"
        host_settings.hub.listen_host = "127.0.0.1"
        host_settings.hub.listen_port = port
        host_repository.save(host_settings, [], [])
        self.host_api = StoryForgeApi(repository=host_repository)
        member = self.host_api.save_software_user(
            {
                "username": "profile-renderer",
                "role": "producer",
                "active": True,
                "initial_password": "Pr1!2026",
            }
        )["data"]

        client_repository = SettingsRepository(self.root / "profile-login-client")
        client_repository.save(AppSettings(), [], [])
        self.client_api = StoryForgeApi(repository=client_repository)
        self.assertEqual(self.client_api._runtime_hub_mode, "local")

        with patch.dict(
            "os.environ",
            {
                "STORYFORGE_HUB_ENDPOINT": self.host_api._hub_server.base_url,  # type: ignore[union-attr]
            },
            clear=False,
        ):
            logged_in = self.client_api.desktop_login(
                "profile-renderer", "Pr1!2026"
            )

        self.assertTrue(logged_in["ok"], logged_in)
        self.assertEqual(logged_in["data"]["user"]["id"], member["id"])
        loaded, _platforms, _batches = client_repository.load()
        self.assertEqual(loaded.hub.mode, "client")
        self.assertEqual(loaded.hub.device_name, socket.gethostname())
        self.assertTrue(loaded.hub.device_id)
        raw = client_repository.settings_path.read_text(encoding="utf-8")
        self.assertNotIn("Pr1!2026", raw)
        self.assertNotIn(loaded.hub.access_token, raw)

    def test_password_login_replaces_legacy_account_token_without_device_id(self) -> None:
        port = _free_port()
        host_repository = SettingsRepository(self.root / "legacy-login-host")
        host_settings = AppSettings()
        host_settings.hub.mode = "host"
        host_settings.hub.listen_host = "127.0.0.1"
        host_settings.hub.listen_port = port
        host_repository.save(host_settings, [], [])
        self.host_api = StoryForgeApi(repository=host_repository)
        member = self.host_api.save_software_user(
            {
                "username": "legacy-renderer",
                "role": "producer",
                "active": True,
                "initial_password": "Lg1!2026",
            }
        )["data"]
        legacy = self.host_api._catalog.issue_hub_access_token(
            member["id"], label="legacy account credential"
        )

        client_repository = SettingsRepository(self.root / "legacy-login-client")
        client_settings = AppSettings()
        client_settings.hub.mode = "client"
        client_settings.hub.endpoint = self.host_api._hub_server.base_url  # type: ignore[union-attr]
        client_settings.hub.access_token = legacy["token"]
        client_settings.hub.device_id = ""
        client_repository.save(client_settings, [], [])
        self.client_api = StoryForgeApi(repository=client_repository)
        self.assertIsNotNone(self.client_api._hub_client)

        with patch(
            "storyforge.worker.ensure_local_worker_autostart",
            return_value={"state": "development", "automatic": False},
        ):
            logged_in = self.client_api.desktop_login(
                "legacy-renderer", "Lg1!2026"
            )

        self.assertTrue(logged_in["ok"], logged_in)
        loaded, _platforms, _batches = client_repository.load()
        self.assertTrue(loaded.hub.device_id)
        self.assertNotEqual(loaded.hub.access_token, legacy["token"])
        self.assertEqual(loaded.hub.account_username, "legacy-renderer")
        raw = client_repository.settings_path.read_text(encoding="utf-8")
        self.assertNotIn("Lg1!2026", raw)
        self.assertNotIn(loaded.hub.access_token, raw)

    def test_registered_device_receives_only_portable_targeted_settings(self) -> None:
        port = _free_port()
        host_repository = SettingsRepository(self.root / "fleet-host")
        host_settings = AppSettings()
        host_settings.hub.mode = "host"
        host_settings.hub.listen_host = "127.0.0.1"
        host_settings.hub.listen_port = port
        host_repository.save(host_settings, [], [])
        self.host_api = StoryForgeApi(repository=host_repository)
        member = self.host_api.save_software_user(
            {
                "username": "fleet-renderer",
                "role": "producer",
                "active": True,
                "initial_password": "Fr1!2026",
            }
        )["data"]

        client_repository = SettingsRepository(self.root / "fleet-client")
        client_settings = AppSettings()
        installation_id = client_settings.hub.installation_id
        client_settings.video_encoder = "h264_nvenc"
        client_settings.providers.tts_endpoint = "http://127.0.0.1:9988"
        client_settings.providers.tts_api_key = "local-device-secret"
        client_repository.save(client_settings, [], [])
        self.client_api = StoryForgeApi(repository=client_repository)
        connected = self.client_api.connect_hub_with_password(
            self.host_api._hub_server.base_url,  # type: ignore[union-attr]
            member["username"],
            "Fr1!2026",
            "Fleet Render PC",
        )
        self.assertTrue(connected["ok"], connected)
        device_id = connected["data"]["device_id"]
        listed = self.host_api.list_managed_devices()
        self.assertEqual(listed["data"]["total"], 1)
        self.assertEqual(listed["data"]["items"][0]["id"], device_id)

        renamed = self.host_api.rename_managed_device(device_id, "Editing Room 2")
        self.assertEqual(renamed["data"]["name"], "Editing Room 2")
        revision = self.host_api.create_managed_device_config(
            {
                "target_mode": "single",
                "device_ids": [device_id],
                "config": {
                    "narration_wpm": 220,
                    "output_fps": 30,
                    "bgm_volume": 0.35,
                    "subtitle": {"font_size": 64},
                },
            }
        )
        self.assertTrue(revision["ok"], revision)
        synced = self.client_api.sync_device_config_now()
        self.assertTrue(synced["ok"], synced)

        loaded, _platforms, _batches = client_repository.load()
        self.assertEqual(loaded.hub.installation_id, installation_id)
        self.assertEqual(loaded.hub.device_id, device_id)
        self.assertEqual(
            loaded.hub.applied_config_revision_id, revision["data"]["id"]
        )
        self.assertEqual(loaded.narration_wpm, 220)
        self.assertEqual(loaded.output_fps, 30)
        self.assertEqual(loaded.bgm_volume, 0.35)
        self.assertEqual(loaded.subtitle.font_size, 64)
        self.assertEqual(loaded.video_encoder, "h264_nvenc")
        self.assertEqual(
            loaded.providers.tts_endpoint, "http://127.0.0.1:9988"
        )
        self.assertEqual(loaded.providers.tts_api_key, "local-device-secret")
        detail = self.host_api.get_managed_device_config(
            revision["data"]["id"]
        )["data"]
        self.assertEqual(detail["targets"][0]["ack_status"], "applied")

        rejected = self.host_api.create_managed_device_config(
            {
                "target_mode": "single",
                "device_ids": [device_id],
                "config": {
                    "providers": {"tts_endpoint": "https://unsafe.invalid"}
                },
            }
        )
        self.assertFalse(rejected["ok"])

        forged = self.client_api.save_settings(
            {
                "hub": {
                    "installation_id": str(uuid4()),
                    "device_id": "forged-device",
                    "applied_config_revision_id": "forged-revision",
                }
            }
        )
        self.assertTrue(forged["ok"], forged)
        protected, _platforms, _batches = client_repository.load()
        self.assertEqual(protected.hub.installation_id, installation_id)
        self.assertEqual(protected.hub.device_id, device_id)
        self.assertEqual(
            protected.hub.applied_config_revision_id, revision["data"]["id"]
        )

    def test_client_library_and_pipeline_text_use_only_the_hub_provider(self) -> None:
        port = _free_port()
        host_repository = SettingsRepository(self.root / "text-host")
        host_settings = AppSettings()
        host_settings.hub.mode = "host"
        host_settings.hub.listen_host = "127.0.0.1"
        host_settings.hub.listen_port = port
        host_settings.providers.text_provider = "groq"
        host_settings.providers.text_endpoint = "https://host-only.invalid/v1"
        host_settings.providers.text_api_key = "host-only-secret"
        host_repository.save(host_settings, [], [])
        self.host_api = StoryForgeApi(repository=host_repository)

        member = self.host_api.save_software_user(
            {
                "username": "text-renderer",
                "display_name": "Text Renderer",
                "role": "producer",
                "active": True,
                "initial_password": "Tr1!2026",
            }
        )
        self.assertTrue(member["ok"], member)
        novel = self.host_api._catalog.import_novel(
            {
                "title": "The Hidden Life",
                "synopsis": "Her husband hid a second life from her.",
                "body": "Her husband hid a second life from her behind a locked door.",
            }
        )["novel"]

        host_calls: list[tuple[object, TextRequest]] = []

        class HostTextProvider:
            def __init__(self, config: object) -> None:
                self.config = config

            def polish(self, request: TextRequest) -> TextResult:
                host_calls.append((self.config, request))
                intro = request.purpose == "intro_card"
                return TextResult(
                    polished_text=(
                        "Her husband hid a second life, and she is about to uncover it."
                        if intro
                        else request.text
                    ),
                    hook="The locked door changes everything.",
                    ending_cta="Continue reading.",
                    mood="suspense",
                    provider="groq",
                    model="host-model",
                    retention_ratio=1.0,
                )

        assert self.host_api._hub_server is not None
        self.host_api._hub_server._text_provider_factory = HostTextProvider

        client_repository = SettingsRepository(self.root / "text-client")
        client_settings = AppSettings()
        client_settings.providers.text_provider = "cloudflare"
        client_settings.providers.text_endpoint = "https://employee.invalid/v1"
        client_settings.providers.text_api_key = "employee-secret-must-not-be-used"
        client_repository.save(client_settings, [], [])
        self.client_api = StoryForgeApi(repository=client_repository)
        connected = self.client_api.connect_hub_with_password(
            self.host_api._hub_server.base_url,
            "text-renderer",
            "Tr1!2026",
            "Text Client PC",
        )
        self.assertTrue(connected["ok"], connected)

        # If client mode ever tries to construct the employee cloud adapter,
        # this patch makes the regression fail immediately.
        with patch(
            "storyforge.api.create_text_provider",
            side_effect=AssertionError("employee cloud text provider was used"),
        ):
            classified = self.client_api.classify_novel(novel["id"], True)
            copy, source = self.client_api._library._intro_card_copy(
                "Her husband hid a second life from her.",
                "The locked door opened.",
                title="The Hidden Life",
                language="English",
            )
            runner = PipelineRunner(
                lambda: self.client_api._state.settings,
                text_provider_factory=self.client_api._runtime_text_provider_factory,
            )
            warnings: list[str] = []
            polished = runner._polish(
                TextRequest(text="The locked door opened."),
                self.client_api._state.settings,
                warnings,
            )

        self.assertTrue(classified["ok"], classified)
        self.assertEqual(
            copy,
            "Her husband hid a second life, and she is about to uncover it.",
        )
        self.assertEqual(source, "novel_synopsis_ai")
        self.assertEqual(polished.provider, "groq")
        self.assertEqual(len(host_calls), 3)
        for config, _request in host_calls:
            self.assertEqual(config.text_api_key, "host-only-secret")
            self.assertEqual(config.text_endpoint, "https://host-only.invalid/v1")
        serialized = json.dumps(
            {
                "classification": classified,
                "copy": copy,
                "polished": polished.to_dict(),
            }
        )
        self.assertNotIn("host-only-secret", serialized)
        self.assertNotIn("employee-secret-must-not-be-used", serialized)

    def test_host_and_client_share_catalog_through_runtime_wiring(self) -> None:
        port = _free_port()
        host_repository = SettingsRepository(self.root / "host")
        host_settings = AppSettings()
        host_settings.hub.mode = "host"
        host_settings.hub.listen_host = "127.0.0.1"
        host_settings.hub.listen_port = port
        platforms = [PlatformProfile(id="goodnovel", name="GoodNovel")]
        host_repository.save(host_settings, platforms, [])

        self.host_api = StoryForgeApi(repository=host_repository)
        host_status = self.host_api.get_hub_status()
        self.assertTrue(host_status["ok"])
        self.assertTrue(host_status["data"]["online"])
        self.assertEqual(host_status["data"]["runtime_mode"], "host")
        self.assertTrue(host_status["data"]["running"])
        self.assertTrue(host_status["data"]["connected"])
        self.assertEqual(host_status["data"]["status"], "ready")
        token = self.host_api._state.settings.hub.access_token
        self.assertTrue(token)

        producer = self.host_api.save_software_user(
            {
                "username": "renderer-one",
                "display_name": "Renderer One",
                "role": "producer",
                "active": True,
            }
        )
        self.assertTrue(producer["ok"], producer)
        issued = self.host_api.issue_hub_user_token(
            producer["data"]["id"], "Studio PC 1"
        )
        self.assertTrue(issued["ok"], issued)
        self.assertTrue(issued["data"]["token"].startswith("sfh_"))
        listed_tokens = self.host_api.list_hub_user_tokens(
            producer["data"]["id"]
        )
        self.assertEqual(listed_tokens["data"]["total"], 1)
        self.assertNotIn("token", listed_tokens["data"]["items"][0])

        client_repository = SettingsRepository(self.root / "client")
        client_settings = AppSettings()
        client_settings.hub.mode = "client"
        client_settings.hub.endpoint = self.host_api._hub_server.base_url  # type: ignore[union-attr]
        client_settings.hub.access_token = token
        client_settings.hub.device_name = "client-one"
        client_repository.save(client_settings, [], [])
        self.client_api = StoryForgeApi(repository=client_repository)

        client_status = self.client_api.get_hub_status()
        self.assertTrue(client_status["data"]["online"])
        self.assertEqual(client_status["data"]["runtime_mode"], "client")
        self.assertTrue(client_status["data"]["connected"])
        self.assertEqual(
            [item.name for item in self.client_api._state.platforms],
            ["GoodNovel"],
        )
        logo = self.root / "client" / "goodnovel-logo.png"
        logo.write_bytes(b"shared-platform-logo")
        saved_platform = self.client_api.save_platform(
            {
                "id": "goodnovel",
                "name": "GoodNovel",
                "logo_path": str(logo),
                "brand_color": "#E53935",
            }
        )
        self.assertTrue(saved_platform["ok"], saved_platform)
        self.assertEqual(
            Path(saved_platform["data"]["logo_path"]).read_bytes(),
            logo.read_bytes(),
        )
        self.assertTrue(saved_platform["data"]["logo_uri"].startswith("file:"))
        render_platform = self.client_api._platform_for_local_render(
            self.client_api._state.platforms[0]
        )
        self.assertEqual(
            Path(render_platform.logo_path).read_bytes(),
            logo.read_bytes(),
        )
        remote_platform = self.host_api._catalog.list_platforms()["items"][0]
        self.assertTrue(
            remote_platform["logo_path"].startswith(
                "hub://attachments/platform-assets/"
            )
        )
        self.assertEqual(remote_platform["brand_color"], "#E53935")
        host_platform = self.host_api.get_bootstrap()["data"]["platforms"][0]
        self.assertEqual(host_platform["brand_color"], "#E53935")
        self.assertEqual(
            Path(host_platform["logo_path"]).read_bytes(), logo.read_bytes()
        )
        self.assertTrue(host_platform["logo_uri"].startswith("file:"))
        imported = self.client_api.import_novel_text(
            {
                "title": "A Shared Midnight",
                "text": "Chapter 1\nAt midnight, the locked phone began to ring.",
            }
        )
        self.assertTrue(imported["ok"], imported)
        self.assertEqual(self.host_api._catalog.list_novels(limit=20)["total"], 1)

        novel_id = imported["data"]["novel"]["id"]
        cover = self.root / "client" / "cover.jpg"
        cover.write_bytes(b"shared-cover-bytes")
        saved_cover = self.client_api.save_novel(
            {"id": novel_id, "cover_path": str(cover)}
        )
        self.assertTrue(saved_cover["ok"], saved_cover)
        self.assertEqual(Path(saved_cover["data"]["cover_path"]).read_bytes(), cover.read_bytes())
        remote_cover = self.host_api._catalog.get_novel(novel_id)["cover_path"]
        self.assertTrue(remote_cover.startswith("hub://attachments/covers/"))
        host_novel = self.host_api.get_novel(novel_id)
        self.assertTrue(host_novel["ok"], host_novel)
        self.assertEqual(
            Path(host_novel["data"]["cover_path"]).read_bytes(),
            cover.read_bytes(),
        )

        novel = self.client_api._catalog.get_novel(novel_id)
        episode_id = novel["current_revision"]["episodes"][0]["id"]
        binding = self.client_api._catalog.save_novel_binding(
            {"novel_id": novel_id, "platform_id": "goodnovel"}
        )
        code = self.client_api._catalog.add_promo_code(
            {"binding_id": binding["id"], "code": "B73165"}
        )
        draft = self.client_api._catalog.save_draft(
            {
                "novel_id": novel_id,
                "binding_id": binding["id"],
                "promo_code_id": code["id"],
                "episode_ids": [episode_id],
                "creative_line_count": 1,
            }
        )
        record = self.client_api._catalog.save_production_record(
            {"draft_id": draft["id"], "device_id": "client-one"}
        )
        sample = self.root / "client" / "sample.mp4"
        sample.write_bytes(b"shared-preview-bytes")
        job = RenderJob(
            batch_id="batch",
            platform_id="goodnovel",
            source_file=str(sample),
            title="A Shared Midnight",
            code="B73165",
            video_folder=str(self.root),
            music_folder=str(self.root),
            output_folder=str(self.root),
            production_record_id=record["id"],
            episode_number=1,
            variant_index=1,
        )
        self.client_api._record_artifact(job, "sample", str(sample))

        cached = self.host_api.get_record_artifacts(record["id"])
        self.assertTrue(cached["ok"], cached)
        artifact = cached["data"]["artifacts"][0]
        self.assertTrue(artifact["available"])
        self.assertEqual(artifact["local_path"], str(sample.resolve()))
        self.assertEqual(
            artifact["metadata"]["storage_scope"], "workstation_local"
        )
        self.assertFalse(artifact["metadata"]["hub_media_uploaded"])
        self.assertNotIn("hub_relative_path", artifact["metadata"])
        self.assertEqual(Path(artifact["cached_path"]).read_bytes(), sample.read_bytes())

        publish_dir = (
            self.root
            / "client"
            / "publish"
            / "GoodNovel_B73165_A Shared Midnight_Bbatch"
        )
        publish_dir.mkdir(parents=True)
        video = publish_dir / "001_GoodNovel_B73165_E001_V01_Bbatch.mp4"
        narration = publish_dir / "001_GoodNovel_B73165_E001_V01_Bbatch.mp3"
        materials_dir = self.root / "client" / "materials"
        materials_dir.mkdir()
        generic_source = materials_dir / "generic-source.mp4"
        video.write_bytes(b"completed-video")
        narration.write_bytes(b"pure-narration-without-bgm")
        generic_source.write_bytes(b"generic-source-video")
        selection = {
            "mode": "generic_fallback",
            "fallback": True,
            "requested_category": "romance",
            "matched_category": None,
            "source_scope": "selected_root_recursive",
        }
        job.publish_batch_folder = str(publish_dir)
        render_work = job_workspace_directory(
            job,
            self.client_api._repository.data_dir / "render-work",
        )
        render_work.mkdir(parents=True)
        (render_work / "manifest.json").write_text(
            json.dumps(
                {
                    "media": {
                        "videos": [str(generic_source)],
                        "video_selection": selection,
                    }
                }
            ),
            encoding="utf-8",
        )
        job.status = JobStatus.COMPLETED
        job.progress = 1.0
        job.output_file = str(video)
        job.narration_audio_file = str(narration)
        # Completion writes are lease-owner protected in the multi-PC runtime.
        # This test bypasses the real queue, so claim the same lease that the
        # queue normally acquires before rendering.
        self.client_api._claim_record_lease(record["id"])
        self.client_api._sync_one_job_record(job)

        completed_artifacts = self.host_api.get_record_artifacts(record["id"])
        self.assertTrue(completed_artifacts["ok"], completed_artifacts)
        narration_artifact = next(
            item
            for item in completed_artifacts["data"]["artifacts"]
            if item["kind"] == "narration"
        )
        self.assertTrue(narration_artifact["available"])
        self.assertEqual(narration_artifact["mime_type"], "audio/mpeg")
        self.assertEqual(
            narration_artifact["local_path"], str(narration.resolve())
        )
        self.assertEqual(
            narration_artifact["metadata"]["storage_scope"],
            "workstation_local",
        )
        self.assertFalse(
            narration_artifact["metadata"]["hub_media_uploaded"]
        )
        self.assertFalse(
            (self.host_api._repository.data_dir / "hub-attachments" / "records").exists()
        )
        self.assertEqual(
            Path(narration_artifact["cached_path"]).read_bytes(),
            narration.read_bytes(),
        )
        recorded = self.host_api._catalog.get_record(record["id"])
        self.assertEqual(
            recorded["metadata"]["narration_audio_file"],
            str(narration),
        )
        self.assertEqual(
            recorded["metadata"]["publish_batch_folder"],
            str(publish_dir),
        )
        self.assertEqual(recorded["metadata"]["media_selection"], selection)
        self.assertTrue(recorded["metadata"]["materials"][0]["generic_fallback"])
        self.assertEqual(
            recorded["metadata"]["materials"][0]["selection_mode"],
            "generic_fallback",
        )
        self.assertEqual(
            {path.suffix for path in publish_dir.iterdir()},
            {".mp4", ".mp3"},
        )

        second_repository = SettingsRepository(self.root / "client-two")
        second_settings = AppSettings()
        second_settings.hub.mode = "client"
        second_settings.hub.endpoint = self.host_api._hub_server.base_url  # type: ignore[union-attr]
        second_settings.hub.access_token = token
        second_settings.hub.device_name = "client-two"
        second_repository.save(second_settings, [], [])
        self.second_client_api = StoryForgeApi(repository=second_repository)
        second_platform = self.second_client_api.get_bootstrap()["data"]["platforms"][0]
        self.assertEqual(second_platform["brand_color"], "#E53935")
        self.assertEqual(
            Path(second_platform["logo_path"]).read_bytes(), logo.read_bytes()
        )
        self.assertTrue(second_platform["logo_uri"].startswith("file:"))
        self.assertTrue(
            second_platform["shared_logo_path"].startswith(
                "hub://attachments/platform-assets/"
            )
        )

        def claim(api: StoryForgeApi) -> tuple[str, str]:
            try:
                return "claimed", api._claim_draft_gate(draft["id"])
            except RuntimeError as error:
                return "blocked", str(error)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(claim, [self.client_api, self.second_client_api])
            )
        self.assertEqual([item[0] for item in outcomes].count("claimed"), 1)
        self.assertEqual([item[0] for item in outcomes].count("blocked"), 1)

    def test_producer_can_share_and_lock_voice_without_editing_novel(self) -> None:
        port = _free_port()
        host_repository = SettingsRepository(self.root / "voice-host")
        host_settings = AppSettings()
        host_settings.hub.mode = "host"
        host_settings.hub.listen_host = "127.0.0.1"
        host_settings.hub.listen_port = port
        host_repository.save(host_settings, [], [])
        self.host_api = StoryForgeApi(repository=host_repository)

        producer = self.host_api.save_software_user(
            {
                "username": "voice-producer",
                "display_name": "Voice Producer",
                "role": "producer",
                "active": True,
            }
        )["data"]
        issued = self.host_api.issue_hub_user_token(
            producer["id"], "Voice workstation"
        )["data"]
        novel = self.host_api._catalog.import_novel(
            {
                "title": "A Managed Midnight",
                "synopsis": "The editor owns this synopsis.",
                "language": "en",
                "body": (
                    "Last night the telephone rang while I was getting ready for bed. "
                    "A woman said she knew my husband and asked why I had never seen "
                    "the locked room. I thought she was lying, but then I opened his "
                    "desk and found a second life hidden inside."
                ),
            }
        )["novel"]

        client_repository = SettingsRepository(self.root / "voice-client")
        client_settings = AppSettings()
        client_settings.hub.mode = "client"
        client_settings.hub.endpoint = self.host_api._hub_server.base_url  # type: ignore[union-attr]
        client_settings.hub.access_token = issued["token"]
        client_settings.hub.device_name = "voice-client"
        client_repository.save(client_settings, [], [])
        self.client_api = StoryForgeApi(repository=client_repository)

        audio = self.root / "voice-client" / "candidate.wav"
        audio.write_bytes(b"voice-preview-bytes")

        def preview_candidates(_text, _mood, _output, *, language="en"):
            return [
                {
                    "profile": "dramatic",
                    "label": "Dramatic",
                    "provider": "local_kokoro",
                    "voice_id": "af_heart",
                    "audio_path": str(audio),
                    "audio_uri": audio.as_uri(),
                    "duration_seconds": 3.0,
                    "excerpt": "Last night the telephone rang.",
                    "language": language,
                    "voice_name": "Heart",
                    "selection_key": "local_kokoro:af_heart:240",
                }
            ]

        self.client_api._library.voice_previews.generate = preview_candidates
        generated = self.client_api.generate_voice_candidates(
            novel["id"], "suspense"
        )
        self.assertTrue(generated["ok"], generated)
        self.assertEqual(generated["data"]["candidates"][0]["voice_id"], "af_heart")
        self.assertEqual(
            generated["data"]["candidates"][0]["selection_key"],
            "local_kokoro:af_heart:240",
        )
        remote = self.host_api._catalog.get_novel(novel["id"])
        remote_candidate = remote["metadata"]["voice_candidates"][0]
        self.assertEqual(
            remote_candidate["selection_key"],
            "local_kokoro:af_heart:240",
        )
        self.assertTrue(
            remote_candidate["audio_path"].startswith(
                "hub://attachments/voice-previews/"
            )
        )

        locked = self.client_api.lock_novel_voice(
            novel["id"],
            {
                "provider": "local_kokoro",
                "voice_id": "af_heart",
                "label": "Dramatic",
                "profile": "dramatic",
            },
        )
        self.assertTrue(locked["ok"], locked)
        self.assertEqual(locked["data"]["locked_voice_id"], "af_heart")

        with self.assertRaises(HubRemoteError) as direct_edit:
            self.client_api._hub_client.call(  # type: ignore[union-attr]
                "save_novel",
                {
                    "value": {
                        "id": novel["id"],
                        "title": "Direct producer overwrite",
                    }
                },
            )
        self.assertEqual(direct_edit.exception.status, 403)

        forbidden = self.client_api.save_novel(
            {
                "id": novel["id"],
                "title": "Producer overwrite",
                "synopsis": "Producer overwrite",
                "cover_path": "producer-cover.jpg",
                "metadata": {"editorial_note": "producer overwrite"},
            }
        )
        self.assertFalse(forbidden["ok"], forbidden)
        unchanged = self.host_api._catalog.get_novel(novel["id"])
        self.assertEqual(unchanged["title"], "A Managed Midnight")
        self.assertEqual(unchanged["synopsis"], "The editor owns this synopsis.")
        self.assertEqual(unchanged["cover_path"], "")
        self.assertNotIn("editorial_note", unchanged["metadata"])

    def test_disconnected_client_opens_settings_but_blocks_shared_writes(self) -> None:
        repository = SettingsRepository(self.root / "offline-client")
        settings = AppSettings()
        settings.hub.mode = "client"
        settings.hub.endpoint = f"http://127.0.0.1:{_free_port()}"
        settings.hub.access_token = "offline-test-token"
        repository.save(settings, [], [])

        self.client_api = StoryForgeApi(repository=repository)
        status = self.client_api.get_hub_status()
        self.assertTrue(status["ok"])
        self.assertFalse(status["data"]["online"])
        self.assertFalse(status["data"]["connected"])
        self.assertEqual(status["data"]["status"], "offline")
        self.assertTrue(status["data"]["error"])
        blocked = self.client_api.import_novel_text(
            {"title": "Must Not Fork", "text": "This must remain unsaved."}
        )
        self.assertFalse(blocked["ok"])
        self.assertIn("不能新建或修改共享数据", blocked["error"])

    def test_restart_reconciliation_does_not_take_live_same_device_lease(self) -> None:
        repository = SettingsRepository(self.root / "recovery-host")
        settings = AppSettings()
        settings.hub.device_name = "recovery-pc"
        repository.save(
            settings,
            [PlatformProfile(id="goodnovel", name="GoodNovel")],
            [],
        )
        self.host_api = StoryForgeApi(repository=repository)
        imported = self.host_api.import_novel_text(
            {
                "title": "A Recoverable Batch",
                "text": "Chapter 1\nThe phone rang after midnight.",
            }
        )["data"]["novel"]
        episode_id = imported["episodes"][0]["id"]
        binding = self.host_api._catalog.save_novel_binding(
            {"novel_id": imported["id"], "platform_id": "goodnovel"}
        )
        code = self.host_api._catalog.add_promo_code(
            {"binding_id": binding["id"], "code": "B73165"}
        )
        draft = self.host_api._catalog.save_draft(
            {
                "novel_id": imported["id"],
                "binding_id": binding["id"],
                "promo_code_id": code["id"],
                "episode_ids": [episode_id],
                "creative_line_count": 1,
                "metadata": {
                    "queue_claim": {
                        "claim_id": "stale-claim",
                        "device_id": "recovery-pc",
                        "expires_at": "2099-01-01T00:00:00+00:00",
                    }
                },
            }
        )
        gate = self.host_api._catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "device_id": "recovery-pc",
                "status": "queued",
                "metadata": {"lease_gate": True, "draft_id": draft["id"]},
            }
        )
        self.host_api._catalog.claim_record_lease(
            gate["id"], "recovery-pc", lease_seconds=180
        )

        self.host_api._reconcile_interrupted_records()

        recovered = next(
            item
            for item in self.host_api._catalog.list_records(limit=20)["items"]
            if item["id"] == gate["id"]
        )
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["lease_owner_device"], "recovery-pc")
        self.assertIsNotNone(recovered["lease_expires_at"])
        self.assertIn(
            "queue_claim", self.host_api._catalog.get_draft(draft["id"])["metadata"]
        )

        with self.host_api._catalog._write_connection() as connection:
            connection.execute(
                "UPDATE production_records SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", gate["id"]),
            )
        self.host_api._reconcile_interrupted_records()
        expired = self.host_api._catalog.get_record(gate["id"])
        self.assertEqual(expired["status"], "interrupted")
        self.assertEqual(expired["lease_owner_device"], "")
        self.assertIsNone(expired["lease_expires_at"])
        self.assertNotIn(
            "queue_claim",
            self.host_api._catalog.get_draft(draft["id"])["metadata"],
        )

        already_interrupted = self.host_api._catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "device_id": "recovery-pc",
                "status": "interrupted",
                "metadata": {"lease_gate": True, "draft_id": draft["id"]},
            }
        )
        self.host_api._catalog.claim_record_lease(
            already_interrupted["id"], "recovery-pc", lease_seconds=180
        )
        with self.host_api._catalog._write_connection() as connection:
            connection.execute(
                "UPDATE production_records SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", already_interrupted["id"]),
            )
        self.host_api._reconcile_interrupted_records()
        recovered_again = next(
            item
            for item in self.host_api._catalog.list_records(limit=20)["items"]
            if item["id"] == already_interrupted["id"]
        )
        self.assertEqual(recovered_again["lease_owner_device"], "")
        self.assertIsNone(recovered_again["lease_expires_at"])


if __name__ == "__main__":
    unittest.main()
