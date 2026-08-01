from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from storyforge.backup import (
    BackupError,
    BackupSecurityError,
    BackupValidationError,
    HubBackupManager,
    _is_reparse_point,
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class HubBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.database_path = self.data_dir / "storyforge-catalog.sqlite3"
        self.clock = MutableClock(
            datetime(2026, 7, 28, 3, 0, tzinfo=timezone.utc)
        )
        self.manager = HubBackupManager(self.data_dir, clock=self.clock)

    def create_catalog(self, rows: int = 3) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES (10, '2026-07-28T00:00:00Z');
            CREATE TABLE stories (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO stories(title) VALUES (?)",
            [(f"Story {index}",) for index in range(rows)],
        )
        connection.commit()
        return connection

    @staticmethod
    def archive_names(path: str | Path) -> set[str]:
        with zipfile.ZipFile(path, "r") as archive:
            return set(archive.namelist())

    @staticmethod
    def extract_catalog(path: str | Path, destination: Path) -> Path:
        with zipfile.ZipFile(path, "r") as archive:
            destination.write_bytes(
                archive.read("data/storyforge-catalog.sqlite3")
            )
        return destination

    def test_wal_database_uses_consistent_sqlite_backup(self) -> None:
        writer = self.create_catalog(rows=5)
        self.addCleanup(writer.close)
        writer.execute("INSERT INTO stories(title) VALUES ('WAL row')")
        writer.commit()
        wal_path = self.database_path.with_name(self.database_path.name + "-wal")
        self.assertTrue(wal_path.is_file())
        self.assertGreater(wal_path.stat().st_size, 0)

        snapshot = self.manager.create_snapshot("daily", cleanup=False)

        self.assertTrue(snapshot["valid"])
        self.assertEqual(snapshot["catalog_schema_version"], 10)
        names = self.archive_names(snapshot["path"])
        self.assertNotIn("data/storyforge-catalog.sqlite3-wal", names)
        self.assertNotIn("data/storyforge-catalog.sqlite3-shm", names)
        extracted = self.extract_catalog(
            snapshot["path"], self.root / "restored.sqlite3"
        )
        restored = sqlite3.connect(extracted)
        try:
            self.assertEqual(
                restored.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
                6,
            )
            self.assertEqual(restored.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            restored.close()

    def test_only_fixed_shared_attachment_groups_are_archived(self) -> None:
        connection = self.create_catalog()
        connection.close()
        (self.data_dir / "settings.json").write_text(
            json.dumps({"schema_version": 13, "settings": {}}),
            encoding="utf-8",
        )
        (self.data_dir / "provider-usage.json").write_text(
            json.dumps({"characters": 42}), encoding="utf-8"
        )
        (self.data_dir / "production-presets.json").write_text(
            json.dumps({"schema_version": 1, "presets": []}), encoding="utf-8"
        )
        attachment_root = self.data_dir / "hub-attachments"
        expected = {
            "covers": "cover.jpg",
            "platform-assets": "logo.png",
            "voice-previews": "voice.wav",
            "preset-assets": "effect.json",
        }
        for group, name in expected.items():
            target = attachment_root / group / "nested" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"{group}-payload".encode("utf-8"))
        (attachment_root / "covers" / "ignored.part").write_bytes(b"partial")
        (attachment_root / "records").mkdir(parents=True)
        (attachment_root / "records" / "employee-narration.wav").write_bytes(
            b"employee artifact"
        )
        for directory, filename in (
            ("web-workspace", "output.mp4"),
            ("production-inputs", "story.txt"),
            ("updates", "full.zip"),
            ("voice-previews", "local.wav"),
            ("covers", "local.jpg"),
        ):
            target = self.data_dir / directory / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"excluded")

        snapshot = self.manager.create_snapshot("manual", cleanup=False)
        names = self.archive_names(snapshot["path"])

        self.assertIn("data/storyforge-catalog.sqlite3", names)
        self.assertIn("data/settings.json", names)
        self.assertIn("data/provider-usage.json", names)
        self.assertIn("data/production-presets.json", names)
        for group, name in expected.items():
            self.assertIn(f"attachments/{group}/nested/{name}", names)
        self.assertFalse(any(name.startswith("attachments/records/") for name in names))
        self.assertFalse(any(name.endswith("ignored.part") for name in names))
        self.assertFalse(any("output.mp4" in name for name in names))
        self.assertFalse(any("full.zip" in name for name in names))

    def test_symlink_or_reparse_source_is_rejected(self) -> None:
        connection = self.create_catalog()
        connection.close()
        suspicious = self.data_dir / "hub-attachments" / "covers" / "linked.jpg"
        suspicious.parent.mkdir(parents=True)
        suspicious.write_bytes(b"looks regular")

        original = _is_reparse_point

        def marked(path: Path) -> bool:
            return path == suspicious or original(path)

        with patch("storyforge.backup._is_reparse_point", side_effect=marked):
            with self.assertRaisesRegex(
                BackupSecurityError, "link or reparse point"
            ):
                self.manager.create_snapshot("daily", cleanup=False)
        self.assertEqual(list(self.manager.backup_dir.glob("*.sfbak")), [])

    def test_tampered_payload_fails_sha256_validation(self) -> None:
        connection = self.create_catalog()
        connection.close()
        (self.data_dir / "settings.json").write_text(
            json.dumps({"schema_version": 13}), encoding="utf-8"
        )
        snapshot = self.manager.create_snapshot("daily", cleanup=False)
        path = Path(snapshot["path"])
        replacement = path.with_suffix(".tampered")

        with zipfile.ZipFile(path, "r") as source:
            values = {
                item.filename: source.read(item)
                for item in source.infolist()
                if not item.is_dir()
            }
        values["data/settings.json"] += b"\nTAMPERED"
        with zipfile.ZipFile(
            replacement, "w", compression=zipfile.ZIP_DEFLATED
        ) as destination:
            for name, payload in values.items():
                destination.writestr(name, payload)
        os.replace(replacement, path)

        with self.assertRaisesRegex(
            BackupValidationError, "(?:size|SHA-256) does not match"
        ):
            self.manager.validate_snapshot(path)
        listed = self.manager.list_snapshots(validate=True)
        self.assertEqual(len(listed), 1)
        self.assertFalse(listed[0]["valid"])

    def test_tampered_manifest_fails_its_own_sha256_validation(self) -> None:
        connection = self.create_catalog()
        connection.close()
        snapshot = self.manager.create_snapshot("daily", cleanup=False)
        path = Path(snapshot["path"])
        replacement = path.with_suffix(".tampered")

        with zipfile.ZipFile(path, "r") as source:
            values = {
                item.filename: source.read(item)
                for item in source.infolist()
                if not item.is_dir()
            }
        manifest = json.loads(values["manifest.json"].decode("utf-8"))
        manifest["reason"] = "manual"
        values["manifest.json"] = json.dumps(manifest).encode("utf-8")
        with zipfile.ZipFile(
            replacement, "w", compression=zipfile.ZIP_DEFLATED
        ) as destination:
            for name, payload in values.items():
                destination.writestr(name, payload)
        os.replace(replacement, path)

        with self.assertRaisesRegex(
            BackupValidationError, "manifest SHA-256 does not match"
        ):
            self.manager.validate_snapshot(path)

    def test_catalog_with_foreign_key_violation_is_not_archived(self) -> None:
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES (10, '2026-07-28T00:00:00Z');
            CREATE TABLE parents (id INTEGER PRIMARY KEY);
            CREATE TABLE children (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES parents(id)
            );
            INSERT INTO children VALUES (1, 999);
            """
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(
            BackupValidationError, "foreign-key violation"
        ):
            self.manager.create_snapshot("daily", cleanup=False)
        self.assertEqual(list(self.manager.backup_dir.glob("*.sfbak")), [])

    def test_cleanup_removes_expired_but_keeps_newest_valid_snapshot(self) -> None:
        connection = self.create_catalog()
        connection.close()
        self.clock.value = datetime(2026, 7, 20, 1, tzinfo=timezone.utc)
        first = self.manager.create_snapshot("daily", cleanup=False)
        with closing(sqlite3.connect(self.database_path)) as changed:
            changed.execute("INSERT INTO stories(title) VALUES ('Changed')")
            changed.commit()
        self.clock.value = datetime(2026, 7, 21, 1, tzinfo=timezone.utc)
        second = self.manager.create_snapshot("pre_upgrade", cleanup=False)
        cleanup_time = datetime(2026, 7, 28, 1, tzinfo=timezone.utc)

        result = self.manager.cleanup(now=cleanup_time)

        self.assertFalse(Path(first["path"]).exists())
        self.assertTrue(Path(second["path"]).is_file())
        self.assertEqual(result["retained"], [second["path"]])
        self.assertEqual(result["removed"], [first["path"]])

    def test_cleanup_retains_all_valid_snapshots_within_72_hours(self) -> None:
        connection = self.create_catalog()
        connection.close()
        now = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
        self.clock.value = now - timedelta(hours=73)
        expired = self.manager.create_snapshot("daily", cleanup=False)
        with closing(sqlite3.connect(self.database_path)) as changed:
            changed.execute("INSERT INTO stories(title) VALUES ('Recent')")
            changed.commit()
        self.clock.value = now - timedelta(hours=71)
        recent = self.manager.create_snapshot("manual", cleanup=False)
        with closing(sqlite3.connect(self.database_path)) as changed:
            changed.execute("INSERT INTO stories(title) VALUES ('Newest')")
            changed.commit()
        self.clock.value = now - timedelta(hours=1)
        newest = self.manager.create_snapshot("pre_bulk_delete", cleanup=False)

        result = self.manager.cleanup(now=now)

        self.assertFalse(Path(expired["path"]).exists())
        self.assertTrue(Path(recent["path"]).is_file())
        self.assertTrue(Path(newest["path"]).is_file())
        self.assertEqual(set(result["retained"]), {recent["path"], newest["path"]})

    def test_list_and_validate_snapshots_by_id(self) -> None:
        connection = self.create_catalog()
        connection.close()
        first = self.manager.create_snapshot("daily", cleanup=False)
        self.clock.value += timedelta(hours=1)
        with closing(sqlite3.connect(self.database_path)) as changed:
            changed.execute("INSERT INTO stories(title) VALUES ('Edited')")
            changed.commit()
        second = self.manager.create_snapshot(
            "manual", metadata={"note": "before edit"}, cleanup=False
        )

        items = self.manager.list_snapshots(validate=True)

        self.assertEqual([item["id"] for item in items], [second["id"], first["id"]])
        checked = self.manager.validate_snapshot(first["id"])
        self.assertEqual(checked["id"], first["id"])
        self.assertTrue(checked["valid"])
        self.assertEqual(items[0]["metadata"], {"note": "before edit"})

    def test_daily_snapshot_is_idempotent_per_utc_date(self) -> None:
        connection = self.create_catalog()
        connection.close()

        first = self.manager.ensure_daily_snapshot()
        duplicate = self.manager.ensure_daily_snapshot()

        self.assertTrue(first["created"])
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["snapshot_id"], first["snapshot_id"])
        self.assertEqual(len(self.manager.list_snapshots(validate=True)), 1)

        self.clock.value += timedelta(days=1)
        next_day = self.manager.ensure_daily_snapshot()

        self.assertFalse(next_day["created"])
        self.assertTrue(next_day["deduplicated"])
        self.assertEqual(next_day["snapshot_id"], first["snapshot_id"])
        self.assertEqual(len(self.manager.list_snapshots(validate=True)), 1)
        status = self.manager.status()
        self.assertEqual(status["last_backup_id"], first["snapshot_id"])
        self.assertEqual(status["last_backup_reason"], "daily")
        self.assertTrue(status["last_deduplicated_at"])
        self.assertFalse(status["last_error"])

        with closing(sqlite3.connect(self.database_path)) as changed:
            changed.execute("INSERT INTO stories(title) VALUES ('Next day change')")
            changed.commit()
        self.clock.value += timedelta(days=1)
        changed_day = self.manager.ensure_daily_snapshot()
        self.assertTrue(changed_day["created"])
        self.assertFalse(changed_day["deduplicated"])
        self.assertEqual(len(self.manager.list_snapshots(validate=True)), 2)

    def test_cleanup_never_keeps_more_than_three_valid_snapshots(self) -> None:
        connection = self.create_catalog()
        connection.close()
        paths: list[Path] = []
        for index in range(5):
            if index:
                with closing(sqlite3.connect(self.database_path)) as changed:
                    changed.execute(
                        "INSERT INTO stories(title) VALUES (?)", (f"Change {index}",)
                    )
                    changed.commit()
            self.clock.value += timedelta(hours=1)
            snapshot = self.manager.create_snapshot("manual", cleanup=False)
            paths.append(Path(snapshot["path"]))

        result = self.manager.cleanup(now=self.clock.value)

        self.assertEqual(len(result["retained"]), 3)
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[1].exists())
        self.assertTrue(all(path.exists() for path in paths[2:]))

    def test_deduplicated_current_content_is_protected_from_cleanup(self) -> None:
        connection = self.create_catalog()
        connection.close()
        snapshots: list[dict[str, object]] = []
        snapshots.append(self.manager.create_snapshot("manual", cleanup=False))
        original_catalog = self.extract_catalog(
            snapshots[0]["path"], self.root / "original-catalog.sqlite3"
        )
        for index in range(1, 4):
            with closing(sqlite3.connect(self.database_path)) as changed:
                changed.execute(
                    "INSERT INTO stories(title) VALUES (?)", (f"Version {index}",)
                )
                changed.commit()
            self.clock.value += timedelta(hours=1)
            snapshots.append(self.manager.create_snapshot("manual", cleanup=False))

        for suffix in ("-wal", "-shm"):
            self.database_path.with_name(self.database_path.name + suffix).unlink(
                missing_ok=True
            )
        os.replace(original_catalog, self.database_path)
        self.clock.value += timedelta(days=4)

        duplicate = self.manager.create_snapshot("manual", cleanup=True)
        remaining = self.manager.list_snapshots(validate=True)

        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(duplicate["id"], snapshots[0]["id"])
        self.assertTrue(Path(str(duplicate["path"])).is_file())
        self.assertLessEqual(len(remaining), 3)
        self.assertIn(snapshots[0]["id"], {item["id"] for item in remaining})

    def test_restore_replaces_authoritative_state_and_preserves_unmanaged_data(
        self,
    ) -> None:
        connection = self.create_catalog(rows=2)
        connection.close()
        (self.data_dir / "settings.json").write_text(
            json.dumps({"schema_version": 13, "name": "snapshot"}),
            encoding="utf-8",
        )
        cover = self.data_dir / "hub-attachments" / "covers" / "cover.jpg"
        cover.parent.mkdir(parents=True)
        cover.write_bytes(b"snapshot-cover")
        snapshot = self.manager.create_snapshot("manual", cleanup=False)

        with closing(sqlite3.connect(self.database_path)) as changed:
            changed.execute("INSERT INTO stories(title) VALUES ('Live edit')")
            changed.commit()
        (self.data_dir / "settings.json").write_text(
            json.dumps({"schema_version": 13, "name": "live"}),
            encoding="utf-8",
        )
        (self.data_dir / "provider-usage.json").write_text(
            json.dumps({"characters": 999}), encoding="utf-8"
        )
        (self.data_dir / "production-presets.json").write_text(
            json.dumps({"presets": ["live-only"]}), encoding="utf-8"
        )
        cover.write_bytes(b"live-cover")
        platform_asset = (
            self.data_dir
            / "hub-attachments"
            / "platform-assets"
            / "live-logo.png"
        )
        platform_asset.parent.mkdir(parents=True)
        platform_asset.write_bytes(b"live-logo")

        unmanaged = {
            self.data_dir
            / "hub-attachments"
            / "records"
            / "employee.wav": b"employee-record",
            self.data_dir / "videos" / "employee.mp4": b"employee-video",
            self.data_dir / "output" / "finished.mp4": b"employee-output",
            self.data_dir / "updates" / "worker.zip": b"update-package",
            self.manager.backup_dir / "operator-note.txt": b"keep-backups",
        }
        for path, payload in unmanaged.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

        real_create_snapshot = self.manager.create_snapshot

        def create_safety_with_stale_sidecars(
            reason: str, **kwargs: object
        ) -> dict[str, object]:
            result = real_create_snapshot(reason, **kwargs)
            if reason == "pre_restore":
                for suffix in ("-wal", "-shm"):
                    self.database_path.with_name(
                        self.database_path.name + suffix
                    ).write_bytes(b"stale-runtime-file")
            return result

        with patch.object(
            self.manager,
            "create_snapshot",
            side_effect=create_safety_with_stale_sidecars,
        ):
            restored = self.manager.restore_snapshot(snapshot["id"])

        self.assertTrue(restored["restored"])
        self.assertTrue(restored["requires_restart"])
        self.assertEqual(restored["snapshot_id"], snapshot["id"])
        self.assertNotEqual(
            restored["pre_restore_snapshot_id"], snapshot["id"]
        )
        with closing(sqlite3.connect(self.database_path)) as checked:
            self.assertEqual(
                checked.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
                2,
            )
        self.assertEqual(
            json.loads((self.data_dir / "settings.json").read_text("utf-8"))[
                "name"
            ],
            "snapshot",
        )
        self.assertFalse((self.data_dir / "provider-usage.json").exists())
        self.assertFalse((self.data_dir / "production-presets.json").exists())
        self.assertEqual(cover.read_bytes(), b"snapshot-cover")
        self.assertFalse(platform_asset.exists())
        for suffix in ("-wal", "-shm"):
            self.assertFalse(
                self.database_path.with_name(
                    self.database_path.name + suffix
                ).exists()
            )
        for path, payload in unmanaged.items():
            self.assertEqual(path.read_bytes(), payload)
        safety = self.manager.validate_snapshot(
            restored["pre_restore_snapshot_id"]
        )
        self.assertEqual(safety["reason"], "pre_restore")
        self.assertEqual(safety["metadata"]["restore_snapshot_id"], snapshot["id"])
        self.assertEqual(self.manager.status()["state"], "restart_required")

    def test_restore_rejects_invalid_snapshot_before_safety_snapshot(self) -> None:
        connection = self.create_catalog()
        connection.close()
        settings = self.data_dir / "settings.json"
        settings.write_text(json.dumps({"name": "snapshot"}), encoding="utf-8")
        snapshot = self.manager.create_snapshot("manual", cleanup=False)
        settings.write_text(json.dumps({"name": "live"}), encoding="utf-8")
        path = Path(snapshot["path"])
        replacement = path.with_suffix(".tampered")
        with zipfile.ZipFile(path, "r") as source:
            values = {
                item.filename: source.read(item)
                for item in source.infolist()
                if not item.is_dir()
            }
        values["data/settings.json"] += b"tampered"
        with zipfile.ZipFile(
            replacement, "w", compression=zipfile.ZIP_DEFLATED
        ) as destination:
            for name, payload in values.items():
                destination.writestr(name, payload)
        os.replace(replacement, path)
        before_archives = set(self.manager.backup_dir.glob("*.sfbak"))

        with self.assertRaises(BackupValidationError):
            self.manager.restore_snapshot(snapshot["id"])

        self.assertEqual(
            json.loads(settings.read_text("utf-8"))["name"], "live"
        )
        self.assertEqual(
            set(self.manager.backup_dir.glob("*.sfbak")), before_archives
        )

    def test_restore_accepts_verified_snapshot_copied_outside_managed_directory(self) -> None:
        connection = self.create_catalog(rows=1)
        connection.close()
        settings = self.data_dir / "settings.json"
        settings.write_text(json.dumps({"name": "snapshot"}), encoding="utf-8")
        snapshot = self.manager.create_snapshot("manual", cleanup=False)
        migrated = self.root / "copied-from-another-computer" / "hub-state.sfbak"
        migrated.parent.mkdir()
        shutil.copy2(snapshot["path"], migrated)
        settings.write_text(json.dumps({"name": "live"}), encoding="utf-8")

        restored = self.manager.restore_snapshot(migrated)

        self.assertTrue(restored["restored"])
        self.assertEqual(Path(restored["snapshot_path"]), migrated.resolve())
        self.assertEqual(json.loads(settings.read_text("utf-8"))["name"], "snapshot")

    def test_external_legacy_zip_is_accepted_only_after_full_validation(self) -> None:
        connection = self.create_catalog()
        connection.close()
        snapshot = self.manager.create_snapshot("manual", cleanup=False)
        legacy = self.root / "legacy-copy.zip"
        shutil.copy2(snapshot["path"], legacy)

        checked = self.manager.validate_snapshot(legacy)

        self.assertTrue(checked["valid"])
        invalid_extension = self.root / "legacy-copy.backup"
        shutil.copy2(snapshot["path"], invalid_extension)
        with self.assertRaisesRegex(BackupValidationError, r"\.sfbak extension"):
            self.manager.validate_snapshot(invalid_extension)

    def test_restore_failure_rolls_back_original_state(self) -> None:
        connection = self.create_catalog(rows=1)
        connection.close()
        settings = self.data_dir / "settings.json"
        settings.write_text(json.dumps({"name": "snapshot"}), encoding="utf-8")
        snapshot = self.manager.create_snapshot("manual", cleanup=False)

        with closing(sqlite3.connect(self.database_path)) as changed:
            changed.execute("INSERT INTO stories(title) VALUES ('Live row')")
            changed.commit()
        settings.write_text(json.dumps({"name": "live"}), encoding="utf-8")
        provider_usage = self.data_dir / "provider-usage.json"
        provider_usage.write_text(
            json.dumps({"characters": 42}), encoding="utf-8"
        )

        real_replace = os.replace

        def fail_during_install(source: object, destination: object) -> None:
            source_path = Path(source)
            if (
                source_path.name == "settings.json"
                and "payload" in source_path.parts
                and any(part.startswith(".restore-") for part in source_path.parts)
            ):
                raise OSError("synthetic restore install failure")
            real_replace(source, destination)

        with patch("storyforge.backup.os.replace", side_effect=fail_during_install):
            with self.assertRaisesRegex(BackupError, "original state was restored"):
                self.manager.restore_snapshot(snapshot["id"])

        with closing(sqlite3.connect(self.database_path)) as checked:
            self.assertEqual(
                checked.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
                2,
            )
        self.assertEqual(json.loads(settings.read_text("utf-8"))["name"], "live")
        self.assertEqual(
            json.loads(provider_usage.read_text("utf-8"))["characters"], 42
        )
        self.assertEqual(list(self.data_dir.glob(".restore-*")), [])
        snapshots = self.manager.list_snapshots(validate=True)
        self.assertEqual(len(snapshots), 2)
        self.assertIn("pre_restore", {item["reason"] for item in snapshots})

    def test_daily_scheduler_starts_and_stops_cleanly(self) -> None:
        connection = self.create_catalog()
        connection.close()

        self.manager.start_daily(check_seconds=0.05)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.manager.status()["last_backup_id"]:
                break
            time.sleep(0.02)

        running = self.manager.status()
        self.assertTrue(running["enabled"])
        self.assertTrue(running["running"])
        self.assertEqual(running["state"], "ready")
        self.assertTrue(running["last_backup_id"])
        self.assertTrue(self.manager.stop_daily(timeout=2))
        stopped = self.manager.health_status()
        self.assertFalse(stopped["enabled"])
        self.assertFalse(stopped["running"])
        self.assertEqual(stopped["state"], "stopped")
        self.assertNotIn("last_error", stopped)
        self.assertIn("has_error", stopped)


if __name__ == "__main__":
    unittest.main()
