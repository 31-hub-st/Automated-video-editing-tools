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
                json.dumps({"settings": {"hub": {"mode": "client"}}}),
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
                "STORYFORGE_ESPEAK_CACHE": "",
                "STORYFORGE_WEBVIEW_DATA_DIR": "",
                "WEBVIEW2_USER_DATA_FOLDER": "",
                "HF_HOME": "",
                "HF_HUB_CACHE": "",
                "TRANSFORMERS_CACHE": "",
                "TORCH_HOME": "",
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
                self.assertEqual(os.environ["HF_HOME"], str(expected / "cache" / "huggingface"))
                self.assertEqual(os.environ["TORCH_HOME"], str(expected / "cache" / "torch"))
                self.assertEqual(os.environ["TEMP"], str(expected / "runtime-temp"))
                self.assertEqual(
                    os.environ["WEBVIEW2_USER_DATA_FOLDER"],
                    str(expected / "webview"),
                )
                self.assertEqual(tempfile.gettempdir(), str(expected / "runtime-temp"))

                self.assertTrue((expected / "settings.json").is_file())
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
