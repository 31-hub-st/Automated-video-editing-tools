from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
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


def make_update_package(
    root: Path,
    version: str = "0.2.0",
    *,
    ui_payload: bytes = b"updated ui",
) -> Path:
    build = Path(tempfile.mkdtemp(prefix=f"update-build-{version}-", dir=root))
    (build / "StoryForge.exe").write_bytes(b"synthetic executable")
    (build / "ui").mkdir()
    (build / "ui" / "index.html").write_bytes(ui_payload)
    (build / "BUILD_STARTUP_VALIDATION.json").write_text(
        json.dumps({"ok": True, "frozen": True, "app_version": version}),
        encoding="utf-8",
    )
    write_release_validation(
        build,
        entrypoint="StoryForge.exe",
        requested_version=version,
        with_local_ai=False,
    )
    package = root / f"StoryForge-{version}.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "storyforge-update.json",
            json.dumps({"version": version, "entrypoint": "StoryForge.exe"}),
        )
        for path in sorted(build.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(build).as_posix())
    return package


def make_unattested_update_package(root: Path, version: str = "0.2.0") -> Path:
    package = root / f"StoryForge-{version}-unattested.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "storyforge-update.json",
            json.dumps({"version": version, "entrypoint": "StoryForge.exe"}),
        )
        archive.writestr("StoryForge.exe", b"synthetic executable")
    return package


def rewrite_update_package(
    source: Path,
    destination: Path,
    replacements: dict[str, bytes],
) -> Path:
    with zipfile.ZipFile(source, "r") as original:
        with zipfile.ZipFile(
            destination, "w", compression=zipfile.ZIP_DEFLATED
        ) as rewritten:
            for entry in original.infolist():
                rewritten.writestr(
                    entry.filename,
                    replacements.get(entry.filename, original.read(entry)),
                )
    return destination


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

    def test_repository_rejects_package_without_release_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")

            with self.assertRaisesRegex(ValueError, "BUILD_RELEASE_VALIDATION"):
                repository.publish(
                    make_unattested_update_package(root),
                    "0.2.0",
                )

    def test_repository_rejects_tampered_release_identity_and_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = make_update_package(root)
            with zipfile.ZipFile(original, "r") as archive:
                validation = json.loads(
                    archive.read("BUILD_RELEASE_VALIDATION.json").decode("utf-8")
                )

            invalid_version = dict(validation)
            invalid_version["app_version"] = "9.9.9"
            invalid_entrypoint = dict(validation)
            invalid_entrypoint["entrypoint"] = "Other.exe"
            variants = {
                "version": {
                    "BUILD_RELEASE_VALIDATION.json": json.dumps(
                        invalid_version
                    ).encode("utf-8")
                },
                "entrypoint": {
                    "BUILD_RELEASE_VALIDATION.json": json.dumps(
                        invalid_entrypoint
                    ).encode("utf-8")
                },
                "bundle": {"ui/index.html": b"tampered ui"},
            }

            for label, replacements in variants.items():
                with self.subTest(label=label):
                    package = rewrite_update_package(
                        original,
                        root / f"tampered-{label}.zip",
                        replacements,
                    )
                    repository = UpdateRepository(root / f"published-{label}")
                    with self.assertRaises(ValueError):
                        repository.publish(package, "0.2.0")

    def test_repository_keeps_current_and_one_recent_previous_package(self) -> None:
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

            self.assertTrue(first_path.is_file())
            self.assertTrue(second_path.is_file())
            self.assertEqual(
                sorted(path.name for path in repository.root.glob("StoryForge-*.zip")),
                sorted([first_path.name, second_path.name]),
            )
            self.assertEqual(repository.get_manifest(), second)

            third = repository.publish(
                make_update_package(root, "0.4.0"), "0.4.0"
            )
            third_path = repository.resolve_package(third)
            self.assertFalse(first_path.exists())
            self.assertTrue(second_path.is_file())
            self.assertTrue(third_path.is_file())
            self.assertEqual(
                len(list(repository.root.glob("StoryForge-*.zip"))),
                2,
            )

    def test_repository_drops_previous_package_after_three_days(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")
            first = repository.publish(
                make_update_package(root, "0.2.0"), "0.2.0"
            )
            first_path = repository.resolve_package(first)
            expired_at = (
                datetime.now(timezone.utc) - timedelta(days=4)
            ).timestamp()
            os.utime(first_path, (expired_at, expired_at))

            second = repository.publish(
                make_update_package(root, "0.3.0"), "0.3.0"
            )

            self.assertFalse(first_path.exists())
            self.assertTrue(repository.resolve_package(second).is_file())

    def test_repository_same_version_publish_is_idempotent_but_rejects_new_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")
            package = make_update_package(root, "0.2.0")

            first = repository.publish(package, "0.2.0", "original notes")
            repeated = repository.publish(package, "0.2.0", "changed notes")

            self.assertEqual(repeated, first)
            self.assertEqual(
                len(list(repository.root.glob("StoryForge-*.zip"))), 1
            )

            alternate_root = root / "alternate"
            alternate_root.mkdir()
            conflicting = make_update_package(
                alternate_root,
                "0.2.0",
                ui_payload=b"different ui bytes",
            )

            with self.assertRaisesRegex(ValueError, "same StoryForge version"):
                repository.publish(conflicting, "0.2.0")

            self.assertEqual(repository.get_manifest(), first)

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
            validation = json.loads(
                (build / "BUILD_RELEASE_VALIDATION.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                validation["bundle_files"],
                [
                    "BUILD_STARTUP_VALIDATION.json",
                    "StoryForge.exe",
                    "ui/index.html",
                ],
            )
            self.assertEqual(
                validation["bundle_file_count"],
                len(validation["bundle_files"]),
            )

    def test_release_validation_rejects_tampered_managed_file_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "build"
            (build / "ui").mkdir(parents=True)
            (build / "StoryForge.exe").write_bytes(b"built application")
            (build / "ui" / "index.html").write_text("updated", encoding="utf-8")
            (build / "BUILD_STARTUP_VALIDATION.json").write_text(
                json.dumps(
                    {"ok": True, "frozen": True, "app_version": __version__}
                ),
                encoding="utf-8",
            )
            write_release_validation(
                build,
                entrypoint="StoryForge.exe",
                requested_version=__version__,
                with_local_ai=False,
            )
            validation_path = build / "BUILD_RELEASE_VALIDATION.json"
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
            validation["bundle_files"].remove("ui/index.html")
            validation_path.write_text(json.dumps(validation), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "bundle_files mismatch"):
                build_package(
                    build,
                    output_path=root / "release" / "tampered-list.zip",
                    entrypoint="StoryForge.exe",
                    version=__version__,
                )

    def test_frozen_package_never_contains_portable_employee_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "build"
            build.mkdir()
            (build / "StoryForge.exe").write_bytes(b"built application")
            (build / "BUILD_STARTUP_VALIDATION.json").write_text(
                json.dumps(
                    {"ok": True, "frozen": True, "app_version": __version__}
                ),
                encoding="utf-8",
            )
            portable = build / "StoryForgeData"
            portable.mkdir()
            (portable / "settings.json").write_text("employee secret", encoding="utf-8")
            write_release_validation(
                build,
                entrypoint="StoryForge.exe",
                requested_version=__version__,
                with_local_ai=False,
            )
            output = root / "release" / "update.zip"

            build_package(
                build,
                output_path=output,
                entrypoint="StoryForge.exe",
                version=__version__,
            )

            with zipfile.ZipFile(output) as archive:
                names = {name.casefold() for name in archive.namelist()}
            self.assertFalse(any(name.startswith("storyforgedata/") for name in names))
            validation = json.loads(
                (build / "BUILD_RELEASE_VALIDATION.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(
                any(
                    path.casefold().startswith("storyforgedata/")
                    for path in validation["bundle_files"]
                )
            )
            self.assertNotIn(
                "build_release_validation.json",
                {path.casefold() for path in validation["bundle_files"]},
            )

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
        self.download_calls = 0

    def get_update_manifest(self) -> dict:
        return dict(self.manifest)

    def download_update_package(self, manifest: dict, *, destination: Path) -> dict:
        self.download_calls += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.package, destination)
        return {
            "path": str(destination),
            "sha256": manifest["sha256"],
            "size_bytes": destination.stat().st_size,
        }


class UpdateManagerTests(unittest.TestCase):
    def test_same_version_manifest_is_not_downloaded_as_an_update(self) -> None:
        """A republished build needs a higher SemVer to reach old clients."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")
            manifest = repository.publish(make_update_package(root), "0.2.0")
            client = _FakeUpdateClient(
                manifest, repository.resolve_package(manifest)
            )
            manager = UpdateManager(
                current_version="0.2.0",
                data_dir=root / "client",
                client_getter=lambda: client,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: False,
            )

            status = manager.check_now(auto_download=True)

            self.assertEqual(status["state"], "up_to_date")
            self.assertEqual(client.download_calls, 0)
            self.assertFalse(manager.pending_path.exists())

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

            health_index = script.index("failed its loopback version health check")
            result_index = script.index("@{ status='installed'")
            marker_removal_index = script.index(
                "Remove-Item -LiteralPath $MarkerPath -Force"
            )
            self.assertLess(health_index, result_index)
            self.assertLess(result_index, marker_removal_index)
            self.assertIn("rollback_available=$true", script)
            self.assertIn("Copy-Item -LiteralPath $backupTarget", script)
            self.assertIn("/worker/api/health", script)
            self.assertIn(
                "[string]$response.data.version -eq $ExpectedVersion", script
            )
            self.assertIn(
                "health='loopback_version_and_install_identity'", script
            )
            self.assertNotIn("health='process_alive'", script)
            self.assertIn("$launched.Refresh()", script)
            self.assertIn("if ($launched.HasExited)", script)
            self.assertIn("$response.data.process_id", script)
            self.assertIn("-ExpectedExecutablePath $expectedWorkerExecutable", script)
            self.assertIn("[string]$workerProcess.Path", script)
            self.assertIn(
                "admin-tools\\enable_storyforge_worker.ps1", script
            )
            self.assertIn("-ExecutablePath $entryPath", script)
            self.assertIn("-RequiredAppVersion $version", script)
            self.assertIn("Unregister-ScheduledTask", script)
            self.assertIn("Export-ScheduledTask", script)
            self.assertIn(
                "Register-ScheduledTask -TaskName $workerTaskName -Xml $existingWorkerTaskXml",
                script,
            )
            self.assertIn("$rollbackScript", script)
            self.assertIn("rollback_service_restored", script)
            self.assertIn(
                "-ExpectedVersion $oldReleaseVersion", script
            )
            self.assertIn("-ExpectedExecutablePath $oldEntryPath", script)
            self.assertNotIn("Wait-StoryForgeWorkerHealth -ExpectedVersion ''", script)
            self.assertIn("BUILD_RELEASE_VALIDATION.json", script)
            self.assertIn("$oldManagedFiles", script)
            self.assertIn("$newManagedSet.Contains($oldRelative)", script)
            self.assertIn("$removed.Add([string]$oldInfo.relative)", script)
            self.assertIn("foreach ($relative in $removed)", script)
            self.assertIn("Protected StoryForgeData path cannot be managed", script)
            self.assertIn("path crosses a reparse point", script)
            self.assertIn(
                "Unknown/user files are intentionally retained".casefold(),
                script.casefold(),
            )
            self.assertNotIn(
                "Get-ChildItem -LiteralPath $installRoot -File -Recurse", script
            )
            launch_index = script.index("$launched = Start-Process")
            worker_health_index = script.index(
                "$workerHealth = Wait-StoryForgeWorkerHealth"
            )
            self.assertLess(launch_index, worker_health_index)
            package_removal_index = script.index(
                "Remove-Item -LiteralPath $package -Force"
            )
            self.assertLess(result_index, package_removal_index)
            self.assertIn("update_storage_root", script)
            unblock_index = script.index("Unblock-File -ErrorAction Stop")
            copy_index = script.index(
                "Copy-Item -LiteralPath $source.FullName -Destination $target -Force"
            )
            self.assertLess(unblock_index, copy_index)
            installing_index = script.index(
                "Add-Member -NotePropertyName installing -NotePropertyValue $true"
            )
            hash_index = script.index("Get-FileHash -LiteralPath $package")
            self.assertLess(installing_index, hash_index)
            self.assertLess(installing_index, copy_index)
            copying_index = script.index(
                "Add-Member -NotePropertyName installing_phase -NotePropertyValue 'copying'"
            )
            health_index = script.index(
                "Add-Member -NotePropertyName installing_phase -NotePropertyValue 'health_check'"
            )
            health_token_index = script.index(
                "$env:STORYFORGE_UPDATE_HEALTH_TOKEN = $installationId"
            )
            self.assertLess(copying_index, copy_index)
            self.assertLess(copy_index, health_index)
            self.assertLess(health_index, launch_index)
            self.assertLess(health_index, health_token_index)
            self.assertLess(health_token_index, launch_index)
            self.assertIn("NotePropertyName installation_id", script)
            self.assertIn("NotePropertyValue 'rollback_health'", script)
            self.assertIn("Local\\StoryForgeUpdate-", script)
            self.assertIn("$installMutex.WaitOne(0)", script)
            self.assertIn("phase='preflight'", script)
            self.assertIn("Remove-Item -LiteralPath $workRoot -Recurse", script)
            obsolete_backup_index = script.index(
                "Copy-Item -LiteralPath $obsoleteTarget -Destination $backupTarget -Force"
            )
            obsolete_remove_index = script.index(
                "Remove-Item -LiteralPath $obsoleteTarget -Force"
            )
            self.assertLess(obsolete_backup_index, obsolete_remove_index)

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

    def test_generated_managed_path_guard_rejects_data_and_escape_paths(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("Windows PowerShell is unavailable")
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
            worker = manager.worker_path.read_text(encoding="utf-8")
            start = worker.index("function Resolve-ManagedInstallPath")
            end = worker.index("function Wait-StoryForgeWorkerHealth")
            resolver = worker[start:end]
            install = root / "StoryForge"
            install.mkdir()
            quoted_install = str(install).replace("'", "''")
            probe = root / "managed-path-probe.ps1"
            probe.write_text(
                resolver
                + "\n$ErrorActionPreference = 'Stop'\n"
                + f"$root = '{quoted_install}'\n"
                + "$valid = Resolve-ManagedInstallPath -Root $root -RelativePath '_internal/ok.dll'\n"
                + "$expected = [IO.Path]::GetFullPath((Join-Path $root '_internal\\ok.dll'))\n"
                + "if (-not [string]::Equals([string]$valid.target, $expected, [StringComparison]::OrdinalIgnoreCase)) { exit 2 }\n"
                + "$rejectedData = $false\n"
                + "try { Resolve-ManagedInstallPath -Root $root -RelativePath 'StoryForgeData/logs/private.log' | Out-Null } catch { $rejectedData = $true }\n"
                + "if (-not $rejectedData) { exit 3 }\n"
                + "$rejectedEscape = $false\n"
                + "try { Resolve-ManagedInstallPath -Root $root -RelativePath '../outside.dll' | Out-Null } catch { $rejectedEscape = $true }\n"
                + "if (-not $rejectedEscape) { exit 4 }\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(probe),
                ],
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

    def test_launch_marks_installing_before_launcher_and_second_manager_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")
            manifest = repository.publish(make_update_package(root), "0.2.0")
            client = _FakeUpdateClient(
                manifest, repository.resolve_package(manifest)
            )
            data_dir = root / "client"
            observations: list[dict] = []

            def launcher(_worker: Path, marker_path: Path, _pid: int) -> None:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                observations.append(marker)
                second = UpdateManager(
                    current_version="0.1.0",
                    data_dir=data_dir,
                    client_getter=lambda: client,
                    mode_getter=lambda: "client",
                    enabled_getter=lambda: True,
                    auto_download_getter=lambda: True,
                    interval_minutes_getter=lambda: 1,
                    rendering_busy_getter=lambda: False,
                )
                self.assertEqual(
                    second.status()["state"], "applying_on_restart"
                )
                self.assertFalse(second.launch_scheduled_update())

            manager = UpdateManager(
                current_version="0.1.0",
                data_dir=data_dir,
                client_getter=lambda: client,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: False,
                launcher=launcher,
            )
            manager.check_now(auto_download=True)

            self.assertTrue(manager.launch_scheduled_update())
            self.assertEqual(len(observations), 1)
            self.assertTrue(observations[0]["installing"])
            self.assertTrue(observations[0]["installing_at"])
            self.assertEqual(observations[0]["installing_phase"], "handoff")
            self.assertRegex(observations[0]["installation_id"], r"^[0-9a-f]{64}$")
            self.assertEqual(observations[0]["apply_work_root"], "")

    def test_launch_preflight_failure_records_marker_result_and_cleans_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")
            manifest = repository.publish(make_update_package(root), "0.2.0")
            client = _FakeUpdateClient(
                manifest, repository.resolve_package(manifest)
            )
            manager = UpdateManager(
                current_version="0.1.0",
                data_dir=root / "client",
                client_getter=lambda: client,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: False,
                launcher=lambda *_args: self.fail("launcher must not run"),
            )
            manager.check_now(auto_download=True)
            apply_root = manager.storage_root / "apply-preflight-test"
            (apply_root / "stage").mkdir(parents=True)

            def fail_preflight(_package: Path, _manifest: dict) -> dict:
                pending = json.loads(
                    manager.pending_path.read_text(encoding="utf-8")
                )
                pending["apply_work_root"] = str(apply_root)
                manager._write_json_atomic(manager.pending_path, pending)
                raise OSError("preflight disk probe failed")

            with (
                patch.object(
                    manager,
                    "_apply_space_requirements",
                    side_effect=fail_preflight,
                ),
                self.assertRaisesRegex(OSError, "preflight disk probe failed"),
            ):
                manager.launch_scheduled_update()

            failed_marker = json.loads(
                manager.pending_path.read_text(encoding="utf-8")
            )
            result = json.loads(manager.result_path.read_text(encoding="utf-8"))
            self.assertFalse(failed_marker["installing"])
            self.assertEqual(failed_marker["installing_at"], "")
            self.assertEqual(failed_marker["installing_phase"], "")
            self.assertEqual(failed_marker["apply_work_root"], "")
            self.assertIn("preflight disk probe failed", failed_marker["last_error"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["phase"], "preflight")
            self.assertIn("preflight disk probe failed", result["error"])
            self.assertFalse(apply_root.exists())
            self.assertEqual(manager.status()["state"], "error")

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

    def test_portable_client_keeps_updates_inside_storyforge_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "StoryForge" / "StoryForgeData"
            with (
                patch("storyforge.updater.sys.frozen", True, create=True),
                patch.dict(
                    os.environ,
                    {"STORYFORGE_PORTABLE_MODE": "1"},
                    clear=False,
                ),
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

            self.assertEqual(manager.storage_root, data_dir / "updates")
            self.assertEqual(manager.cache_root, data_dir / "updates" / "downloads")

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

    def test_backup_estimate_includes_only_obsolete_verified_release_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install"
            (install / "ui").mkdir(parents=True)
            (install / "StoryForge.exe").write_bytes(b"old")
            (install / "ui" / "index.html").write_bytes(b"old-ui")
            (install / "obsolete.dll").write_bytes(b"obsolete")
            (install / "employee-notes.txt").write_bytes(b"keep-me")
            old_validation = {
                "bundle_file_count": 3,
                "bundle_files": [
                    "StoryForge.exe",
                    "ui/index.html",
                    "obsolete.dll",
                ],
            }
            old_validation_path = install / "BUILD_RELEASE_VALIDATION.json"
            old_validation_path.write_text(
                json.dumps(old_validation), encoding="utf-8"
            )
            package = root / "update.zip"
            new_validation = {
                "bundle_file_count": 2,
                "bundle_files": ["StoryForge.exe", "ui/index.html"],
            }
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr(
                    "storyforge-update.json",
                    json.dumps(
                        {"version": "0.2.0", "entrypoint": "StoryForge.exe"}
                    ),
                )
                archive.writestr("StoryForge.exe", b"new")
                archive.writestr("ui/index.html", b"new-ui")
                archive.writestr(
                    "BUILD_RELEASE_VALIDATION.json",
                    json.dumps(new_validation),
                )
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

            with patch.object(manager, "install_root", return_value=install):
                estimated = manager._estimate_backup_bytes(package)

            expected = sum(
                path.stat().st_size
                for path in (
                    install / "StoryForge.exe",
                    install / "ui" / "index.html",
                    install / "obsolete.dll",
                    old_validation_path,
                )
            )
            self.assertEqual(estimated, expected)
            self.assertNotEqual(
                estimated,
                expected + (install / "employee-notes.txt").stat().st_size,
            )

    def test_auto_download_waits_for_idle_before_copying_or_inspecting(self) -> None:
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
            self.assertEqual(status["state"], "available")
            self.assertFalse(status["downloaded"])
            self.assertFalse(status["apply_on_restart"])
            self.assertEqual(status["package_path"], "")
            self.assertFalse(manager.pending_path.exists())
            self.assertEqual(client.download_calls, 0)
            self.assertFalse(manager.launch_scheduled_update())
            self.assertEqual(launches, [])

            busy[0] = False
            status = manager.check_now(auto_download=True)
            self.assertEqual(status["state"], "scheduled")
            self.assertTrue(status["downloaded"])
            self.assertTrue(status["apply_on_restart"])
            self.assertTrue(Path(status["package_path"]).is_file())
            self.assertTrue(manager.pending_path.is_file())
            self.assertEqual(client.download_calls, 1)
            self.assertTrue(manager.launch_scheduled_update())
            self.assertEqual(len(launches), 1)
            self.assertTrue(launches[0][0].is_file())
            self.assertTrue(launches[0][1].is_file())
            marker = json.loads(launches[0][1].read_text(encoding="utf-8"))
            self.assertTrue(marker["register_local_worker"])

    def test_manual_download_also_defers_while_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")
            manifest = repository.publish(make_update_package(root), "0.2.0")
            client = _FakeUpdateClient(
                manifest, repository.resolve_package(manifest)
            )
            busy = [True]
            manager = UpdateManager(
                current_version="0.1.0",
                data_dir=root / "client",
                client_getter=lambda: client,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: busy[0],
            )

            status = manager.download()

            self.assertEqual(status["state"], "available")
            self.assertTrue(status["rendering_busy"])
            self.assertFalse(status["downloaded"])
            self.assertEqual(client.download_calls, 0)
            self.assertFalse(manager.pending_path.exists())

            busy[0] = False
            status = manager.download()
            self.assertEqual(status["state"], "scheduled")
            self.assertEqual(client.download_calls, 1)
            self.assertTrue(manager.pending_path.is_file())

    def test_download_defers_when_render_reservation_is_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = UpdateRepository(root / "published")
            manifest = repository.publish(make_update_package(root), "0.2.0")
            client = _FakeUpdateClient(
                manifest, repository.resolve_package(manifest)
            )
            reservation = threading.Lock()
            reservation.acquire()
            manager = UpdateManager(
                current_version="0.1.0",
                data_dir=root / "client",
                client_getter=lambda: client,
                mode_getter=lambda: "client",
                enabled_getter=lambda: True,
                auto_download_getter=lambda: True,
                interval_minutes_getter=lambda: 1,
                rendering_busy_getter=lambda: False,
                heavy_resource_lock=reservation,
            )

            try:
                status = manager.check_now(auto_download=True)
            finally:
                reservation.release()

            self.assertEqual(status["state"], "available")
            self.assertFalse(status["downloaded"])
            self.assertEqual(client.download_calls, 0)
            self.assertFalse(manager.pending_path.exists())

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
