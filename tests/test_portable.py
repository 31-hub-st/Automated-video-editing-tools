from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from storyforge.portable import (
    MIGRATION_LOCK,
    MIGRATION_MARKER,
    _cleanup_legacy_employee_data,
    configure_runtime_environment,
    migrate_legacy_employee_data,
    should_use_portable_data,
)


class PortableEmployeeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_tempdir = tempfile.tempdir

    def tearDown(self) -> None:
        tempfile.tempdir = self.previous_tempdir

    def test_source_and_frozen_hub_keep_legacy_appdata_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "StoryForge" / "StoryForge Studio.exe"
            executable.parent.mkdir()
            (executable.parent / "storyforge-connection.json").write_text(
                "{}", encoding="utf-8"
            )
            environment = {
                "APPDATA": str(root / "Roaming"),
                "LOCALAPPDATA": str(root / "Local"),
                "STORYFORGE_DATA_DIR": "",
                "STORYFORGE_PORTABLE_MODE": "",
            }
            with patch.dict(os.environ, environment, clear=False):
                self.assertFalse(
                    should_use_portable_data([], frozen=False, executable=executable)
                )
                self.assertFalse(
                    should_use_portable_data(
                        ["--web"], frozen=True, executable=executable
                    )
                )
                self.assertIsNone(
                    configure_runtime_environment(
                        ["--web"], frozen=True, executable=executable
                    )
                )
                self.assertFalse((executable.parent / "StoryForgeData").exists())

    def test_frozen_employee_never_migrates_a_legacy_hub_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "Employee Install"
            install.mkdir()
            executable = install / "StoryForge Studio.exe"
            executable.write_bytes(b"exe")
            (install / "storyforge-connection.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "endpoint": "http://10.0.0.225:8765",
                    }
                ),
                encoding="utf-8",
            )

            roaming = root / "Roaming"
            local = root / "Local"
            legacy = roaming / "StoryForgeStudio"
            legacy.mkdir(parents=True)
            (legacy / "settings.json").write_text(
                json.dumps(
                    {
                        "schema_version": 14,
                        "settings": {
                            "language": "legacy-host-language",
                            "hub": {
                                "mode": "host",
                                "endpoint": "http://127.0.0.1:8765",
                                "access_token": "protected-host-token",
                                "account_username": "hub-admin",
                                "installation_id": "hub-installation",
                                "device_id": "hub-device",
                                "applied_config_revision_id": "hub-revision",
                                "applied_config_hash": "hub-config-hash",
                                "listen_host": "0.0.0.0",
                                "listen_port": 8765,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            database = legacy / "storyforge-catalog.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE host_only(value TEXT)")
                connection.execute("INSERT INTO host_only VALUES ('must-not-copy')")
                connection.commit()
            finally:
                connection.close()
            host_cache = legacy / "cache" / "host-cache.bin"
            host_cache.parent.mkdir()
            host_cache.write_bytes(b"must remain with the legacy Hub")
            legacy_local = local / "StoryForgeStudio"
            legacy_local.mkdir(parents=True)
            local_payload = legacy_local / "host-runtime.json"
            local_payload.write_text("{}", encoding="utf-8")

            environment = {
                "APPDATA": str(roaming),
                "LOCALAPPDATA": str(local),
                "STORYFORGE_DATA_DIR": "",
                "STORYFORGE_PORTABLE_MODE": "",
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch("storyforge.portable._active_legacy_worker", return_value=False),
            ):
                configured = configure_runtime_environment(
                    [], frozen=True, executable=executable
                )

            expected = install / "StoryForgeData"
            self.assertEqual(configured, expected)
            self.assertFalse((expected / "settings.json").exists())
            self.assertFalse((expected / "storyforge-catalog.sqlite3").exists())
            self.assertFalse((expected / "host-runtime.json").exists())
            self.assertTrue(database.is_file())
            self.assertTrue(host_cache.is_file())
            self.assertTrue(local_payload.is_file())
            marker = json.loads(
                (expected / MIGRATION_MARKER).read_text(encoding="utf-8")
            )
            self.assertTrue(marker["completed"])
            self.assertEqual(marker["copied_file_count"], 0)
            self.assertEqual(
                {Path(item) for item in marker["skipped_legacy_host_roots"]},
                {legacy.resolve(), legacy_local.resolve()},
            )

    def test_frozen_employee_skips_legacy_catalog_when_role_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roaming = root / "Roaming"
            local = root / "Local"
            legacy = roaming / "StoryForgeStudio"
            legacy_local = local / "StoryForgeStudio"
            legacy.mkdir(parents=True)
            legacy_local.mkdir(parents=True)
            (legacy / "settings.json").write_text(
                '{"settings":{"hub":{"mode":"unknown"}',
                encoding="utf-8",
            )
            database = legacy / "storyforge-catalog.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE authority(value TEXT)")
                connection.execute("INSERT INTO authority VALUES ('must-not-copy')")
                connection.commit()
            finally:
                connection.close()
            local_payload = legacy_local / "host-runtime.json"
            local_payload.write_text("{}", encoding="utf-8")
            target = root / "Employee" / "StoryForgeData"
            target.mkdir(parents=True)

            with (
                patch.dict(
                    os.environ,
                    {
                        "APPDATA": str(roaming),
                        "LOCALAPPDATA": str(local),
                    },
                    clear=False,
                ),
                patch("storyforge.portable._active_legacy_worker", return_value=False),
            ):
                result = migrate_legacy_employee_data(target)

            self.assertTrue(result["completed"])
            self.assertEqual(result["legacy_skip_reason"], "unclassified_catalog_role")
            self.assertEqual(result["sources"], [])
            self.assertEqual(result["copied_file_count"], 0)
            self.assertEqual(
                {Path(item) for item in result["skipped_legacy_roots"]},
                {legacy.resolve(), legacy_local.resolve()},
            )
            self.assertFalse((target / "settings.json").exists())
            self.assertFalse((target / "storyforge-catalog.sqlite3").exists())
            self.assertFalse((target / "host-runtime.json").exists())
            self.assertTrue(database.is_file())
            self.assertTrue(local_payload.is_file())

    def test_legacy_local_role_still_migrates_its_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roaming = root / "Roaming"
            legacy = roaming / "StoryForgeStudio"
            legacy.mkdir(parents=True)
            (legacy / "settings.json").write_text(
                json.dumps({"settings": {"hub": {"mode": "local"}}}),
                encoding="utf-8",
            )
            database = legacy / "storyforge-catalog.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE local_work(value TEXT)")
                connection.execute("INSERT INTO local_work VALUES ('migrated')")
                connection.commit()
            finally:
                connection.close()
            target = root / "Employee" / "StoryForgeData"
            target.mkdir(parents=True)

            with (
                patch.dict(
                    os.environ,
                    {
                        "APPDATA": str(roaming),
                        "LOCALAPPDATA": str(root / "Local"),
                    },
                    clear=False,
                ),
                patch("storyforge.portable._active_legacy_worker", return_value=False),
            ):
                result = migrate_legacy_employee_data(target)

            self.assertTrue(result["completed"])
            self.assertEqual(result["legacy_skip_reason"], "")
            self.assertTrue((target / "settings.json").is_file())
            migrated = sqlite3.connect(target / "storyforge-catalog.sqlite3")
            try:
                self.assertEqual(
                    migrated.execute("SELECT value FROM local_work").fetchone()[0],
                    "migrated",
                )
            finally:
                migrated.close()

    def test_frozen_employee_routes_every_runtime_cache_beside_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "Employee Selected Folder"
            install.mkdir()
            executable = install / "StoryForge Studio.exe"
            executable.write_bytes(b"exe")
            (install / "storyforge-connection.json").write_text(
                "{}", encoding="utf-8"
            )
            roaming = root / "Roaming"
            local = root / "Local"
            legacy = roaming / "StoryForgeStudio"
            legacy.mkdir(parents=True)
            (legacy / "settings.json").write_text(
                json.dumps(
                    {
                        "settings": {
                            "hub": {
                                "mode": "client",
                                "endpoint": "http://10.0.0.225:8765",
                                "access_token": "protected-client-token",
                                "account_username": "renderer",
                                "installation_id": "employee-installation",
                                "device_id": "employee-device",
                                "listen_host": "127.0.0.1",
                                "listen_port": 18000,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (legacy / "runtime-logs").mkdir()
            (legacy / "runtime-logs" / "worker.log").write_text(
                "kept", encoding="utf-8"
            )
            # Active/transient files stay in legacy AppData and are never
            # copied into a freshly started queue.
            (legacy / "updates").mkdir()
            (legacy / "updates" / "pending.zip").write_bytes(b"old update")
            (legacy / "render-work").mkdir()
            (legacy / "render-work" / "partial.mp4").write_bytes(b"partial")
            (legacy / "render-work" / "manifest.json").write_text(
                "{}", encoding="utf-8"
            )
            (legacy / "render-work" / "job-error.log").write_text(
                "diagnostic", encoding="utf-8"
            )
            (legacy / "render-work" / "render-command.txt").write_text(
                "ffmpeg ...", encoding="utf-8"
            )
            (legacy / "render-work" / "unknown.bin").write_bytes(b"keep")
            previews = legacy / "render-work" / ".previews"
            previews.mkdir()
            (previews / "sample.mp4").write_bytes(b"preview")
            (previews / "render-error.log").write_text(
                "keep error", encoding="utf-8"
            )
            database = legacy / "storyforge-catalog.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE sample(value TEXT)")
                connection.execute("INSERT INTO sample VALUES ('migrated')")
                connection.commit()
            finally:
                connection.close()

            environment = {
                "APPDATA": str(roaming),
                "LOCALAPPDATA": str(local),
                "STORYFORGE_DATA_DIR": "",
                "STORYFORGE_PORTABLE_MODE": "",
                "STORYFORGE_TTS_CACHE_DIR": "",
                "STORYFORGE_MEDIA_INDEX_PATH": "",
                "STORYFORGE_ESPEAK_CACHE": "",
                "STORYFORGE_WEBVIEW_DATA_DIR": "",
                "WEBVIEW2_USER_DATA_FOLDER": "",
                "HF_HOME": "",
                "HF_HUB_CACHE": "",
                "TRANSFORMERS_CACHE": "",
                "TORCH_HOME": "",
                "TORCHINDUCTOR_CACHE_DIR": "",
                "TRITON_CACHE_DIR": "",
                "NUMBA_CACHE_DIR": "",
                "MPLCONFIGDIR": "",
                "PIP_CACHE_DIR": "",
                "UV_CACHE_DIR": "",
                "XDG_DATA_HOME": "",
                "XDG_CONFIG_HOME": "",
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch("storyforge.portable._active_legacy_worker", return_value=False),
            ):
                data = configure_runtime_environment(
                    [], frozen=True, executable=executable
                )
                expected = install / "StoryForgeData"
                self.assertEqual(data, expected)
                self.assertEqual(os.environ["STORYFORGE_DATA_DIR"], str(expected))
                self.assertEqual(
                    os.environ["STORYFORGE_TTS_CACHE_DIR"],
                    str(expected / "cache" / "tts"),
                )
                self.assertEqual(
                    os.environ["STORYFORGE_MEDIA_INDEX_PATH"],
                    str(expected / "cache" / "media-index.sqlite3"),
                )
                self.assertEqual(os.environ["HF_HOME"], str(expected / "cache" / "huggingface"))
                self.assertEqual(os.environ["TORCH_HOME"], str(expected / "cache" / "torch"))
                self.assertEqual(os.environ["PIP_CACHE_DIR"], str(expected / "cache" / "pip"))
                self.assertEqual(
                    os.environ["TORCHINDUCTOR_CACHE_DIR"],
                    str(expected / "cache" / "torchinductor"),
                )
                self.assertEqual(os.environ["XDG_DATA_HOME"], str(expected / "data"))
                self.assertEqual(os.environ["TEMP"], str(expected / "runtime-temp"))
                self.assertEqual(
                    os.environ["WEBVIEW2_USER_DATA_FOLDER"],
                    str(expected / "webview"),
                )
                self.assertEqual(tempfile.gettempdir(), str(expected / "runtime-temp"))

                self.assertTrue((expected / "settings.json").is_file())
                migrated_settings = json.loads(
                    (expected / "settings.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    migrated_settings["settings"]["hub"],
                    {
                        "mode": "client",
                        "endpoint": "http://10.0.0.225:8765",
                        "access_token": "protected-client-token",
                        "account_username": "renderer",
                        "installation_id": "employee-installation",
                        "device_id": "employee-device",
                        "listen_host": "127.0.0.1",
                        "listen_port": 18000,
                    },
                )
                self.assertTrue((expected / "runtime-logs" / "worker.log").is_file())
                self.assertFalse((expected / "updates" / "pending.zip").exists())
                self.assertFalse((expected / "render-work" / "partial.mp4").exists())
                self.assertTrue((legacy / "settings.json").is_file())
                self.assertFalse((legacy / "updates").exists())
                self.assertFalse((legacy / "render-work" / "partial.mp4").exists())
                self.assertFalse((previews / "sample.mp4").exists())
                self.assertTrue((legacy / "render-work" / "manifest.json").is_file())
                self.assertTrue((legacy / "render-work" / "job-error.log").is_file())
                self.assertTrue((legacy / "render-work" / "render-command.txt").is_file())
                self.assertTrue((legacy / "render-work" / "unknown.bin").is_file())
                self.assertTrue((previews / "render-error.log").is_file())
                marker = json.loads(
                    (expected / MIGRATION_MARKER).read_text(encoding="utf-8")
                )
                self.assertTrue(marker["completed"])
                self.assertTrue(marker["legacy_preserved"])
                self.assertTrue(marker["legacy_cleanup"]["attempted"])
                self.assertTrue(marker["legacy_cleanup"]["completed"])
                self.assertFalse(marker["cleanup_pending"])
                self.assertGreater(marker["legacy_cleanup"]["released_bytes"], 0)
                connection = sqlite3.connect(expected / "storyforge-catalog.sqlite3")
                try:
                    self.assertEqual(
                        connection.execute("SELECT value FROM sample").fetchone()[0],
                        "migrated",
                    )
                finally:
                    connection.close()

    def test_frozen_employee_rejects_non_ascii_install_before_creating_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "员工制作电脑"
            install.mkdir()
            executable = install / "StoryForge Studio.exe"
            executable.write_bytes(b"exe")
            (install / "storyforge-connection.json").write_text(
                "{}", encoding="utf-8"
            )
            # A drive-root cache was used by an earlier workaround.  It must
            # not be created now that all employee state belongs to one root.
            external_fallback = Path(executable.drive + "\\") / "StoryForgeRuntime"
            existed_before = external_fallback.exists()
            environment = {
                "APPDATA": str(root / "Roaming"),
                "LOCALAPPDATA": str(root / "Local"),
                "STORYFORGE_DATA_DIR": "",
                "STORYFORGE_PORTABLE_MODE": "",
            }

            with patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(RuntimeError, r"D:\\StoryForge"):
                    configure_runtime_environment(
                        [], frozen=True, executable=executable
                    )

            self.assertFalse((install / "StoryForgeData").exists())
            self.assertEqual(external_fallback.exists(), existed_before)

    def test_legacy_cleanup_refuses_an_unexpected_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roaming = root / "Roaming"
            local = root / "Local"
            outside = root / "outside"
            cache = outside / "cache"
            cache.mkdir(parents=True)
            payload = cache / "must-remain.bin"
            payload.write_bytes(b"do not delete")

            with (
                patch.dict(
                    os.environ,
                    {
                        "APPDATA": str(roaming),
                        "LOCALAPPDATA": str(local),
                    },
                    clear=False,
                ),
                patch("storyforge.portable._active_legacy_worker", return_value=False),
            ):
                result = _cleanup_legacy_employee_data((outside,))

            self.assertTrue(payload.is_file())
            self.assertFalse(result["completed"])
            self.assertTrue(
                any("unexpected legacy root" in item for item in result["errors"])
            )

    def test_migration_never_overwrites_newer_portable_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "install" / "StoryForgeData"
            target.mkdir(parents=True)
            (target / "settings.json").write_text("new", encoding="utf-8")
            legacy = root / "Roaming" / "StoryForgeStudio"
            legacy.mkdir(parents=True)
            (legacy / "settings.json").write_text("old", encoding="utf-8")
            with (
                patch.dict(
                    os.environ,
                    {
                        "APPDATA": str(root / "Roaming"),
                        "LOCALAPPDATA": str(root / "Local"),
                    },
                    clear=False,
                ),
                patch("storyforge.portable._active_legacy_worker", return_value=False),
            ):
                result = migrate_legacy_employee_data(target)

            self.assertTrue(result["completed"])
            self.assertEqual((target / "settings.json").read_text(encoding="utf-8"), "new")
            self.assertEqual((legacy / "settings.json").read_text(encoding="utf-8"), "old")

    def test_completed_migration_retries_only_unfinished_legacy_cleanup(self) -> None:
        unfinished_states = (
            ("missing", None),
            (
                "worker-skipped",
                {
                    "attempted": False,
                    "completed": False,
                    "skipped_reason": "legacy_worker_active",
                    "errors": [],
                },
            ),
            (
                "cleanup-error",
                {
                    "attempted": True,
                    "completed": False,
                    "skipped_reason": "",
                    "errors": ["cache: PermissionError: busy"],
                },
            ),
        )
        for label, previous_cleanup in unfinished_states:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / "install" / "StoryForgeData"
                target.mkdir(parents=True)
                roaming = root / "Roaming"
                local = root / "Local"
                legacy = roaming / "StoryForgeStudio"
                cache_file = legacy / "cache" / "retry.bin"
                cache_file.parent.mkdir(parents=True)
                cache_file.write_bytes(b"regenerable")
                marker = {
                    "schema_version": 1,
                    "completed": True,
                    "sources": [str(legacy)],
                    "copied_file_count": 1,
                    "copied_files": ["settings.json"],
                    "legacy_preserved": True,
                    "errors": [],
                    "cleanup_pending": True,
                }
                if previous_cleanup is not None:
                    marker["legacy_cleanup"] = previous_cleanup
                (target / MIGRATION_MARKER).write_text(
                    json.dumps(marker), encoding="utf-8"
                )

                with (
                    patch.dict(
                        os.environ,
                        {
                            "APPDATA": str(roaming),
                            "LOCALAPPDATA": str(local),
                        },
                        clear=False,
                    ),
                    patch(
                        "storyforge.portable._active_legacy_worker",
                        return_value=False,
                    ),
                    patch(
                        "storyforge.portable._copy_missing_file",
                        side_effect=AssertionError("must not repeat durable copy"),
                    ),
                ):
                    result = migrate_legacy_employee_data(target)

                self.assertTrue(result["completed"])
                self.assertFalse(result["cleanup_pending"])
                self.assertTrue(result["legacy_cleanup"]["completed"])
                self.assertIn("cleanup_retried_at", result)
                self.assertFalse(cache_file.exists())
                persisted = json.loads(
                    (target / MIGRATION_MARKER).read_text(encoding="utf-8")
                )
                self.assertFalse(persisted["cleanup_pending"])

    def test_migration_copy_error_remains_terminal_and_never_runs_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "StoryForgeData"
            target.mkdir()
            failed = {
                "schema_version": 1,
                "completed": False,
                "sources": [str(Path(temporary) / "Roaming" / "StoryForgeStudio")],
                "errors": ["settings.json: PermissionError: denied"],
            }
            (target / MIGRATION_MARKER).write_text(
                json.dumps(failed), encoding="utf-8"
            )

            with patch(
                "storyforge.portable._cleanup_legacy_employee_data",
                side_effect=AssertionError("failed copy must stay failed"),
            ):
                result = migrate_legacy_employee_data(target)

            self.assertEqual(result, failed)

    def test_live_legacy_worker_defers_migration_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "StoryForgeData"
            target.mkdir()
            with patch("storyforge.portable._active_legacy_worker", return_value=True):
                result = migrate_legacy_employee_data(target)

            self.assertTrue(result["deferred"])
            self.assertEqual(result["reason"], "legacy_worker_active")
            self.assertFalse((target / MIGRATION_MARKER).exists())

    def test_second_process_waits_for_migration_owner_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "StoryForgeData"
            target.mkdir()
            lock = target / MIGRATION_LOCK
            lock.write_text("first-process", encoding="ascii")
            marker = target / MIGRATION_MARKER

            def finish_migration() -> None:
                time.sleep(0.15)
                marker.write_text(
                    json.dumps({"completed": True, "errors": []}),
                    encoding="utf-8",
                )
                lock.unlink()

            thread = threading.Thread(target=finish_migration)
            thread.start()
            try:
                with patch(
                    "storyforge.portable._active_legacy_worker",
                    return_value=False,
                ):
                    result = migrate_legacy_employee_data(
                        target, wait_seconds=2.0
                    )
            finally:
                thread.join(timeout=2.0)

            self.assertTrue(result["completed"])
            self.assertFalse(thread.is_alive())

    def test_migration_lock_timeout_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "StoryForgeData"
            target.mkdir()
            (target / MIGRATION_LOCK).write_text("stuck", encoding="ascii")

            with patch(
                "storyforge.portable._active_legacy_worker",
                return_value=False,
            ):
                result = migrate_legacy_employee_data(target, wait_seconds=0.05)

            self.assertFalse(result["completed"])
            self.assertEqual(result["reason"], "migration_lock_timeout")
            self.assertTrue(result["errors"])

    def test_explicit_data_override_is_honored_in_source_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "custom-data"
            with patch.dict(
                os.environ,
                {
                    "STORYFORGE_DATA_DIR": str(target),
                    "STORYFORGE_PORTABLE_MODE": "1",
                },
                clear=False,
            ):
                configured = configure_runtime_environment([], frozen=False)
                self.assertEqual(configured, target)
                self.assertEqual(os.environ["TEMP"], str(target / "runtime-temp"))
                self.assertNotIn("STORYFORGE_PORTABLE_MODE", os.environ)

    def test_authorized_frozen_hub_gui_is_not_employee_portable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "Hub"
            install.mkdir()
            executable = install / "StoryForge Studio.exe"
            executable.write_bytes(b"exe")
            (install / "storyforge-connection.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "endpoint": "http://10.0.0.225:8765",
                    }
                ),
                encoding="utf-8",
            )
            target = root / "HubData"
            authorized = str(target.resolve(strict=False))
            with patch.dict(
                os.environ,
                {
                    "STORYFORGE_DATA_DIR": authorized,
                    "STORYFORGE_FROZEN_HUB_DATA_ROOT": authorized,
                    "STORYFORGE_DEPLOYMENT_ROLE": "Hub",
                    "STORYFORGE_PORTABLE_MODE": "1",
                },
                clear=False,
            ):
                configured = configure_runtime_environment(
                    [], frozen=True, executable=executable
                )

                self.assertEqual(configured, target.resolve(strict=False))
                self.assertNotIn("STORYFORGE_PORTABLE_MODE", os.environ)
                self.assertFalse((target / MIGRATION_MARKER).exists())

    def test_mismatched_hub_authorization_remains_employee_portable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "Employee"
            install.mkdir()
            executable = install / "StoryForge Studio.exe"
            executable.write_bytes(b"exe")
            (install / "storyforge-connection.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "endpoint": "http://10.0.0.225:8765",
                    }
                ),
                encoding="utf-8",
            )
            target = root / "EmployeeData"
            with (
                patch.dict(
                    os.environ,
                    {
                        "STORYFORGE_DATA_DIR": str(target),
                        "STORYFORGE_FROZEN_HUB_DATA_ROOT": str(root / "OtherData"),
                        "STORYFORGE_PORTABLE_MODE": "",
                    },
                    clear=False,
                ),
                patch(
                    "storyforge.portable.migrate_legacy_employee_data",
                    return_value={"completed": True, "errors": []},
                ) as migrate,
            ):
                configured = configure_runtime_environment(
                    [], frozen=True, executable=executable
                )

                self.assertEqual(configured, target.resolve(strict=False))
                self.assertEqual(os.environ["STORYFORGE_PORTABLE_MODE"], "1")
                migrate.assert_not_called()

    def test_read_only_selected_root_has_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary) / "not-a-folder"
            blocker.write_text("file", encoding="utf-8")
            target = blocker / "StoryForgeData"
            with patch.dict(
                os.environ,
                {"STORYFORGE_DATA_DIR": str(target)},
                clear=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "D 盘或 E 盘"):
                    configure_runtime_environment([], frozen=False)


if __name__ == "__main__":
    unittest.main()
