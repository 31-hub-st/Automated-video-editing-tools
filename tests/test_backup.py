from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from storyforge.backup import (
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
        self.clock.value = now - timedelta(hours=71)
        recent = self.manager.create_snapshot("manual", cleanup=False)
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

        self.assertTrue(next_day["created"])
        self.assertEqual(len(self.manager.list_snapshots(validate=True)), 2)
        status = self.manager.status()
        self.assertEqual(status["last_backup_id"], next_day["snapshot_id"])
        self.assertEqual(status["last_backup_reason"], "daily")
        self.assertFalse(status["last_error"])

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
