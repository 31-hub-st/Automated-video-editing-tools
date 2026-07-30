from __future__ import annotations

import json
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path

from storyforge import __version__
from storyforge.api import StoryForgeApi
from storyforge.config import SettingsRepository
from storyforge.models import AppSettings


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def package(root: Path, version: str) -> Path:
    target = root / f"StoryForge-{version}.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(
            "storyforge-update.json",
            json.dumps({"version": version, "entrypoint": "StoryForge.exe"}),
        )
        archive.writestr("StoryForge.exe", b"new executable")
    return target


class ApiUpdateRuntimeTests(unittest.TestCase):
    def test_host_publishes_and_client_downloads_without_live_overwrite(self) -> None:
        self.assertEqual(__version__, "0.4.0-rc7")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            port = free_port()
            host_repository = SettingsRepository(root / "host")
            host_settings = AppSettings()
            host_settings.hub.mode = "host"
            host_settings.hub.listen_host = "127.0.0.1"
            host_settings.hub.listen_port = port
            host_repository.save(host_settings, [], [])
            host = StoryForgeApi(repository=host_repository)
            client: StoryForgeApi | None = None
            try:
                published = host.publish_update(
                    str(package(root, "0.4.1")),
                    "0.4.1",
                    "Editable cards and captions",
                )
                self.assertTrue(published["ok"], published)
                self.assertEqual(
                    published["data"]["manifest"]["version"], "0.4.1"
                )

                client_repository = SettingsRepository(root / "client")
                client_settings = AppSettings()
                client_settings.hub.mode = "client"
                client_settings.hub.endpoint = f"http://127.0.0.1:{port}"
                client_settings.hub.access_token = host._state.settings.hub.access_token
                # Keep this test deterministic; manual checks still exercise
                # the exact same signed download path as the monitor thread.
                client_settings.hub.auto_update_enabled = False
                client_settings.hub.auto_download_updates = True
                client_repository.save(client_settings, [], [])
                client = StoryForgeApi(repository=client_repository)

                checked = client.check_for_updates()
                self.assertTrue(checked["ok"], checked)
                status = checked["data"]
                self.assertEqual(status["state"], "scheduled")
                self.assertEqual(status["available_version"], "0.4.1")
                self.assertTrue(Path(status["package_path"]).is_file())
                self.assertTrue(status["apply_on_restart"])

                scheduled = client.schedule_update_on_restart()
                self.assertTrue(scheduled["ok"], scheduled)
                self.assertEqual(scheduled["data"]["state"], "scheduled")
                self.assertTrue(scheduled["data"]["apply_on_restart"])
                cancelled = client.cancel_scheduled_update()
                self.assertTrue(cancelled["ok"], cancelled)
                self.assertFalse(cancelled["data"]["apply_on_restart"])
                self.assertFalse(client._update_manager.pending_path.exists())

                cleared = host.clear_published_update()
                self.assertTrue(cleared["ok"], cleared)
                self.assertEqual(cleared["data"]["state"], "publisher_idle")
            finally:
                if client is not None:
                    client._shutdown()
                host._shutdown()


if __name__ == "__main__":
    unittest.main()
