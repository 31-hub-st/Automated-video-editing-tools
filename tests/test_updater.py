from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from storyforge.catalog import CatalogRepository
from storyforge.config import ApplicationState, SettingsRepository
from storyforge.hub import HubClient, HubServer
from storyforge.models import AppSettings
from storyforge.updater import (
    UPDATE_MANIFEST_SCHEMA,
    UpdateManager,
    UpdateRepository,
    file_sha256,
    inspect_update_package,
    is_newer_version,
)
from scripts.build_update_package import build_package


def make_update_package(root: Path, version: str = "0.2.0") -> Path:
    package = root / f"StoryForge-{version}.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "storyforge-update.json",
            json.dumps({"version": version, "entrypoint": "StoryForge.exe"}),
        )
        archive.writestr("StoryForge.exe", b"synthetic executable")
        archive.writestr("ui/index.html", b"updated ui")
    return package


class UpdatePackageTests(unittest.TestCase):
    def test_version_comparison_handles_final_and_prerelease(self) -> None:
        self.assertTrue(is_newer_version("0.2.0", "0.1.9"))
        self.assertTrue(is_newer_version("1.0.0", "1.0.0-rc.2"))
        self.assertFalse(is_newer_version("1.0.0-rc.2", "1.0.0"))

    def test_repository_publishes_only_verified_self_describing_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = make_update_package(root)
            repository = UpdateRepository(root / "published")

            manifest = repository.publish(package, "0.2.0", "New styles")

            self.assertEqual(manifest["schema_version"], UPDATE_MANIFEST_SCHEMA)
            self.assertEqual(manifest["version"], "0.2.0")
            self.assertEqual(manifest["entrypoint"], "StoryForge.exe")
            self.assertEqual(repository.get_manifest(), manifest)
            self.assertEqual(
                file_sha256(repository.resolve_package(manifest)),
                manifest["sha256"],
            )

    def test_update_settings_default_to_one_minute_and_validate_interval(self) -> None:
        self.assertTrue(AppSettings().hub.auto_update_enabled)
        self.assertTrue(AppSettings().hub.auto_download_updates)
        self.assertEqual(AppSettings().hub.update_check_minutes, 1)
        with tempfile.TemporaryDirectory() as temporary:
            state = ApplicationState(SettingsRepository(Path(temporary)))
            state.update_settings(
                {
                    "hub": {
                        "auto_update_enabled": False,
                        "auto_download_updates": False,
                        "update_check_minutes": 15,
                    }
                }
            )
            self.assertFalse(state.settings.hub.auto_update_enabled)
            self.assertEqual(state.settings.hub.update_check_minutes, 15)
            with self.assertRaisesRegex(ValueError, "更新检查间隔"):
                state.update_settings({"hub": {"update_check_minutes": 0}})

    def test_build_script_writes_self_describing_package_and_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "build"
            build.mkdir()
            (build / "StoryForge.exe").write_bytes(b"built application")
            (build / "ui").mkdir()
            (build / "ui" / "index.html").write_text("updated", encoding="utf-8")
            output = root / "release" / "StoryForge-0.2.0-update.zip"

            result = build_package(
                build,
                output_path=output,
                entrypoint="StoryForge.exe",
                version="0.2.0",
            )

            self.assertTrue(output.is_file())
            self.assertTrue(Path(result["manifest_path"]).is_file())
            self.assertEqual(inspect_update_package(output)["version"], "0.2.0")

    def test_package_validation_rejects_zip_slip_and_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            malicious = root / "malicious.zip"
            with zipfile.ZipFile(malicious, "w") as archive:
                archive.writestr(
                    "storyforge-update.json",
                    json.dumps(
                        {"version": "0.2.0", "entrypoint": "StoryForge.exe"}
                    ),
                )
                archive.writestr("../StoryForge.exe", b"escape")
            with self.assertRaisesRegex(ValueError, "更新包内部"):
                inspect_update_package(malicious)

            valid = make_update_package(root, "0.2.0")
            with self.assertRaisesRegex(ValueError, "版本"):
                inspect_update_package(valid, expected_version="0.3.0")


class HubUpdateTransportTests(unittest.TestCase):
    def test_authenticated_manifest_and_package_are_verified_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            attachments = root / "attachments"
            data.mkdir()
            attachments.mkdir()
            catalog = CatalogRepository(root / "catalog.sqlite3")
            actor = catalog.save_user({"username": "owner", "role": "admin"})
            repository = UpdateRepository(root / "published")
            manifest = repository.publish(
                make_update_package(root), "0.2.0", "Release notes"
            )
            server = HubServer(
                catalog,
                {"update-token": actor["id"]},
                host="127.0.0.1",
                port=0,
                data_root=data,
                attachment_root=attachments,
                update_repository=repository,
            ).start()
            self.addCleanup(server.stop)
            client = HubClient(server.base_url, "update-token", timeout_seconds=5)

            remote = client.get_update_manifest()
            self.assertEqual(remote, manifest)
            destination = root / "download" / manifest["filename"]
            downloaded = client.download_update_package(
                remote or {}, destination=destination
            )

            self.assertEqual(downloaded["sha256"], manifest["sha256"])
            self.assertEqual(file_sha256(destination), manifest["sha256"])
            self.assertEqual(
                inspect_update_package(destination)["version"], "0.2.0"
            )

    def test_hub_signs_an_empty_update_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            catalog = CatalogRepository(root / "catalog.sqlite3")
            actor = catalog.save_user({"username": "owner", "role": "admin"})
            server = HubServer(
                catalog,
                {"update-token": actor["id"]},
                host="127.0.0.1",
                port=0,
                data_root=data,
                update_repository=UpdateRepository(root / "published"),
            ).start()
            self.addCleanup(server.stop)

            self.assertIsNone(
                HubClient(server.base_url, "update-token").get_update_manifest()
            )


class _FakeUpdateClient:
    def __init__(self, manifest: dict, package: Path) -> None:
        self.manifest = manifest
        self.package = package

    def get_update_manifest(self) -> dict:
        return dict(self.manifest)

    def download_update_package(self, manifest: dict, *, destination: Path) -> dict:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.package, destination)
        return {
            "path": str(destination),
            "sha256": manifest["sha256"],
            "size_bytes": destination.stat().st_size,
        }


class UpdateManagerTests(unittest.TestCase):
    def test_auto_download_schedules_and_never_applies_during_render(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")
            manifest = repository.publish(make_update_package(root), "0.2.0")
            client = _FakeUpdateClient(
                manifest, repository.resolve_package(manifest)
            )
            busy = [True]
            launches: list[tuple[Path, Path, int]] = []
            manager = UpdateManager(
                current_version="0.1.0",
                data_dir=root / "client",
                client_getter=lambda: client,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: busy[0],
                launcher=lambda worker, marker, pid: launches.append(
                    (worker, marker, pid)
                ),
            )

            status = manager.check_now(auto_download=True)
            self.assertEqual(status["state"], "deferred")
            self.assertTrue(status["downloaded"])
            self.assertTrue(status["apply_on_restart"])
            self.assertTrue(Path(status["package_path"]).is_file())
            self.assertTrue(manager.pending_path.is_file())
            self.assertFalse(manager.launch_scheduled_update())
            self.assertEqual(launches, [])

            busy[0] = False
            self.assertTrue(manager.launch_scheduled_update())
            self.assertEqual(len(launches), 1)
            self.assertTrue(launches[0][0].is_file())
            self.assertTrue(launches[0][1].is_file())

    def test_tampered_download_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")
            manifest = repository.publish(make_update_package(root), "0.2.0")
            tampered = root / "tampered.zip"
            tampered.write_bytes(b"not the signed package")
            client = _FakeUpdateClient(manifest, tampered)
            manager = UpdateManager(
                current_version="0.1.0",
                data_dir=root / "client",
                client_getter=lambda: client,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: False,
            )

            with self.assertRaisesRegex(ValueError, "大小|SHA-256"):
                manager.check_now(auto_download=True)
            downloads = list((root / "client" / "updates" / "downloads").rglob("*.zip"))
            self.assertEqual(downloads, [])


if __name__ == "__main__":
    unittest.main()
