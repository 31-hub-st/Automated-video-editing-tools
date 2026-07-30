from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from storyforge import __version__
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
from scripts.build_update_package import build_package, write_release_validation


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
        self.assertTrue(is_newer_version("1.0.0-rc.10", "1.0.0-rc.7"))

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

    def test_repository_keeps_only_the_current_published_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")

            first = repository.publish(
                make_update_package(root, "0.2.0"), "0.2.0"
            )
            first_path = repository.resolve_package(first)
            self.assertTrue(first_path.is_file())

            second = repository.publish(
                make_update_package(root, "0.3.0"), "0.3.0"
            )
            second_path = repository.resolve_package(second)

            self.assertFalse(first_path.exists())
            self.assertTrue(second_path.is_file())
            self.assertEqual(
                [path.name for path in repository.root.glob("StoryForge-*.zip")],
                [second_path.name],
            )
            self.assertEqual(repository.get_manifest(), second)

    def test_locked_stale_package_does_not_fail_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")
            # A directory with the package suffix gives deterministic unlink
            # failure on every supported platform, equivalent to a ZIP being
            # locked by antivirus or an active Windows download.
            locked = repository.root / "StoryForge-locked.zip"
            locked.mkdir()

            manifest = repository.publish(
                make_update_package(root, "0.4.0"), "0.4.0"
            )

            self.assertTrue(repository.resolve_package(manifest).is_file())
            self.assertEqual(repository.get_manifest(), manifest)
            self.assertTrue(locked.is_dir())

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
            (build / "BUILD_STARTUP_VALIDATION.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "frozen": True,
                        "app_version": __version__,
                    }
                ),
                encoding="utf-8",
            )
            (build / "ui").mkdir()
            (build / "ui" / "index.html").write_text("updated", encoding="utf-8")
            write_release_validation(
                build,
                entrypoint="StoryForge.exe",
                requested_version=__version__,
                with_local_ai=False,
            )
            output = root / "release" / f"StoryForge-{__version__}-update.zip"

            result = build_package(
                build,
                output_path=output,
                entrypoint="StoryForge.exe",
                version=__version__,
            )

            self.assertTrue(output.is_file())
            self.assertTrue(Path(result["manifest_path"]).is_file())
            self.assertEqual(inspect_update_package(output)["version"], __version__)

    def test_frozen_package_rejects_entrypoint_changed_after_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "build"
            build.mkdir()
            entrypoint = build / "StoryForge.exe"
            entrypoint.write_bytes(b"verified executable")
            (build / "BUILD_STARTUP_VALIDATION.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "frozen": True,
                        "app_version": __version__,
                    }
                ),
                encoding="utf-8",
            )
            write_release_validation(
                build,
                entrypoint="StoryForge.exe",
                requested_version=__version__,
                with_local_ai=False,
            )
            entrypoint.write_bytes(b"different executable")

            with self.assertRaisesRegex(ValueError, "entrypoint changed"):
                build_package(
                    build,
                    output_path=root / "release" / "changed.zip",
                    entrypoint="StoryForge.exe",
                    version=__version__,
                )

    def test_full_local_ai_attestation_requires_passed_kokoro_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "build"
            build.mkdir()
            (build / "StoryForge.exe").write_bytes(b"verified executable")
            (build / "BUILD_STARTUP_VALIDATION.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "frozen": True,
                        "app_version": __version__,
                    }
                ),
                encoding="utf-8",
            )
            kokoro = build / "local-ai" / "kokoro"
            (kokoro / "voices").mkdir(parents=True)
            (kokoro / "kokoro-v1_0.pth").write_bytes(b"model")
            (kokoro / "config.json").write_text("{}", encoding="utf-8")
            (kokoro / "voices" / "af_heart.pt").write_bytes(b"voice")

            with self.assertRaisesRegex(ValueError, "BUILD_KOKORO_VALIDATION"):
                write_release_validation(
                    build,
                    entrypoint="StoryForge.exe",
                    requested_version=__version__,
                    with_local_ai=True,
                )

    def test_full_local_ai_package_rechecks_kokoro_and_bundle_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "build"
            build.mkdir()
            (build / "StoryForge.exe").write_bytes(b"verified executable")
            validation = {
                "ok": True,
                "frozen": True,
                "app_version": __version__,
            }
            (build / "BUILD_STARTUP_VALIDATION.json").write_text(
                json.dumps(validation), encoding="utf-8"
            )
            (build / "BUILD_KOKORO_VALIDATION.json").write_text(
                json.dumps(validation), encoding="utf-8"
            )
            kokoro = build / "local-ai" / "kokoro"
            (kokoro / "voices").mkdir(parents=True)
            model = kokoro / "kokoro-v1_0.pth"
            model.write_bytes(b"verified model")
            (kokoro / "config.json").write_text("{}", encoding="utf-8")
            (kokoro / "voices" / "af_heart.pt").write_bytes(b"voice")
            write_release_validation(
                build,
                entrypoint="StoryForge.exe",
                requested_version=__version__,
                with_local_ai=True,
            )
            model.write_bytes(b"changed model")

            with self.assertRaisesRegex(ValueError, "build directory changed"):
                build_package(
                    build,
                    output_path=root / "release" / "changed-ai.zip",
                    entrypoint="StoryForge.exe",
                    version=__version__,
                )

    def test_build_script_rejects_relabeling_an_old_frozen_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "old-build"
            build.mkdir()
            (build / "StoryForge.exe").write_bytes(b"old executable")
            (build / "BUILD_STARTUP_VALIDATION.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "frozen": True,
                        "app_version": __version__,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError, "validated binary.*requested package"
            ):
                build_package(
                    build,
                    output_path=root / "release" / "relabelled.zip",
                    entrypoint="StoryForge.exe",
                    version="999.0.0",
                )

    def test_frozen_storyforge_package_requires_passed_build_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "unverified-build"
            build.mkdir()
            (build / "StoryForge.exe").write_bytes(b"unverified executable")

            with self.assertRaisesRegex(
                ValueError, "BUILD_STARTUP_VALIDATION.json"
            ):
                build_package(
                    build,
                    output_path=root / "release" / "unverified.zip",
                    entrypoint="StoryForge.exe",
                    version=__version__,
                )

    def test_source_package_uses_imported_source_version_without_frozen_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source-build"
            source.mkdir()
            (source / "run.py").write_text("print('ok')\n", encoding="utf-8")
            output = root / "release" / "source-update.zip"

            result = build_package(
                source,
                output_path=output,
                entrypoint="run.py",
                version=__version__,
            )

            self.assertTrue(output.is_file())
            self.assertEqual(result["manifest"]["version"], __version__)

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
    def test_apply_script_commits_only_after_startup_health_and_keeps_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = UpdateManager(
                current_version="0.1.0",
                data_dir=root / "client",
                client_getter=lambda: None,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: False,
            )

            manager._write_windows_worker()
            script = manager.worker_path.read_text(encoding="utf-8")

            health_index = script.index("failed its startup health check")
            result_index = script.index("@{ status='installed'")
            marker_removal_index = script.index(
                "Remove-Item -LiteralPath $MarkerPath -Force"
            )
            self.assertLess(health_index, result_index)
            self.assertLess(result_index, marker_removal_index)
            self.assertIn("rollback_available=$true", script)
            self.assertIn("Copy-Item -LiteralPath $backupTarget", script)
            package_removal_index = script.index(
                "Remove-Item -LiteralPath $package -Force"
            )
            self.assertLess(result_index, package_removal_index)
            self.assertIn("update_storage_root", script)

    def test_generated_apply_script_is_valid_windows_powershell(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("Windows PowerShell parser is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = UpdateManager(
                current_version="0.1.0",
                data_dir=root / "client",
                client_getter=lambda: None,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: False,
            )
            manager._write_windows_worker()
            quoted = str(manager.worker_path).replace("'", "''")
            command = (
                "$tokens=$null; $errors=$null; "
                f"[System.Management.Automation.Language.Parser]::ParseFile('{quoted}', "
                "[ref]$tokens, [ref]$errors) | Out-Null; "
                "if ($errors.Count -gt 0) { $errors | ForEach-Object { "
                "[Console]::Error.WriteLine($_.Message) }; exit 1 }"
            )

            completed = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_source_mode_keeps_deterministic_data_directory_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = UpdateManager(
                current_version="0.1.0",
                data_dir=root / "client",
                client_getter=lambda: None,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: False,
            )

            self.assertEqual(manager.storage_root, manager.data_dir / "updates")

    def test_new_process_does_not_reapply_or_rehash_installing_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")
            manifest = repository.publish(make_update_package(root), "0.2.0")
            package = repository.resolve_package(manifest)
            data_dir = root / "client"
            pending = data_dir / "updates" / "pending-update.json"
            pending.parent.mkdir(parents=True)
            active_apply = data_dir / "updates" / "apply-active"
            (active_apply / "backup").mkdir(parents=True)
            (active_apply / "backup" / "StoryForge.exe").write_bytes(b"old")
            stale_apply = data_dir / "updates" / "apply-stale"
            stale_apply.mkdir(parents=True)
            pending.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "manifest": manifest,
                        "package_path": str(package),
                        "install_root": str(root / "install"),
                        "installing": True,
                        "installing_at": datetime.now(timezone.utc).isoformat(),
                        "apply_work_root": str(active_apply),
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "storyforge.updater.file_sha256",
                side_effect=AssertionError("must not rehash installer-owned zip"),
            ):
                manager = UpdateManager(
                    current_version="0.1.0",
                    data_dir=data_dir,
                    client_getter=lambda: None,
                    mode_getter=lambda: "client",
                    enabled_getter=lambda: True,
                    auto_download_getter=lambda: True,
                    interval_minutes_getter=lambda: 1,
                    rendering_busy_getter=lambda: False,
                )

            status = manager.status()
            self.assertEqual(status["state"], "applying_on_restart")
            self.assertFalse(status["apply_on_restart"])
            self.assertFalse(manager.launch_scheduled_update())
            self.assertTrue(package.is_file())
            self.assertTrue(active_apply.is_dir())
            self.assertFalse(stale_apply.exists())

    def test_frozen_client_prefers_writable_install_volume_for_large_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_root = root / "employee-drive" / "StoryForge"
            install_root.mkdir(parents=True)
            with (
                patch("storyforge.updater.sys.frozen", True, create=True),
                patch.object(UpdateManager, "install_root", return_value=install_root),
            ):
                manager = UpdateManager(
                    current_version="0.1.0",
                    data_dir=root / "appdata" / "client",
                    client_getter=lambda: None,
                    mode_getter=lambda: "client",
                    enabled_getter=lambda: True,
                    auto_download_getter=lambda: True,
                    interval_minutes_getter=lambda: 1,
                    rendering_busy_getter=lambda: False,
                )

            self.assertEqual(manager.storage_root.parent, install_root.parent)
            self.assertNotEqual(manager.storage_root, manager.state_root)
            self.assertEqual(manager.cache_root.parent, manager.storage_root)

    def test_cleanup_preserves_pending_zip_and_removes_superseded_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = UpdateManager(
                current_version="0.1.0",
                data_dir=root / "client",
                client_getter=lambda: None,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: False,
            )
            pending = manager.cache_root / "0.2.0" / "pending.zip"
            pending.parent.mkdir(parents=True)
            pending.write_bytes(b"pending")
            stale = manager.cache_root / "0.1.9" / "stale.zip"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale")
            manager._write_json_atomic(
                manager.pending_path,
                {"package_path": str(pending)},
            )

            manager._cleanup_update_storage()

            self.assertTrue(pending.is_file())
            self.assertFalse(stale.exists())
            self.assertFalse(stale.parent.exists())

    def test_cleanup_lock_failure_is_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = UpdateManager(
                current_version="0.1.0",
                data_dir=root / "client",
                client_getter=lambda: None,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: False,
            )
            locked = manager.cache_root / "locked-version"
            locked.mkdir(parents=True)
            (locked / "locked.zip").write_bytes(b"locked")

            with patch(
                "storyforge.updater.shutil.rmtree",
                side_effect=PermissionError("antivirus lock"),
            ):
                manager._cleanup_update_storage()

            self.assertTrue(locked.is_dir())

    def test_cleanup_keeps_latest_rollback_through_exact_three_day_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = UpdateManager(
                current_version="0.1.0",
                data_dir=root / "client",
                client_getter=lambda: None,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: False,
            )
            now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
            retained = manager.storage_root / "apply-retained"
            backup = retained / "backup"
            backup.mkdir(parents=True)
            (backup / "StoryForge.exe").write_bytes(b"rollback")
            stale = manager.storage_root / "apply-stale"
            (stale / "backup").mkdir(parents=True)
            manager._write_json_atomic(
                manager.result_path,
                {
                    "status": "installed",
                    "installed_at": (now - timedelta(days=3)).isoformat(),
                    "backup_path": str(backup),
                    "rollback_available": True,
                },
            )

            manager._cleanup_update_storage(now=now)
            self.assertTrue(retained.is_dir())
            self.assertFalse(stale.exists())

            manager._cleanup_update_storage(now=now + timedelta(microseconds=1))
            self.assertFalse(retained.exists())
            result = json.loads(manager.result_path.read_text(encoding="utf-8"))
            self.assertFalse(result["rollback_available"])
            self.assertEqual(result["backup_path"], "")

    def test_download_space_preflight_rejects_low_disk_before_network_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")
            manifest = repository.publish(make_update_package(root), "0.2.0")
            manager = UpdateManager(
                current_version="0.1.0",
                data_dir=root / "client",
                client_getter=lambda: None,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: False,
            )

            with (
                patch(
                    "storyforge.updater.shutil.disk_usage",
                    return_value=SimpleNamespace(total=1, used=1, free=0),
                ),
                self.assertRaisesRegex(OSError, "空间不足"),
            ):
                manager._ensure_download_space(manifest)

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
