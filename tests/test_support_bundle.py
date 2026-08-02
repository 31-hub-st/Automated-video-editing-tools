from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from storyforge import __version__
from storyforge.api import StoryForgeApi
from storyforge.support_bundle import (
    cleanup_local_storage,
    create_support_bundle,
    inspect_local_storage,
)


def _write(path: Path, payload: bytes, *, age_days: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    if age_days:
        timestamp = time.time() - age_days * 24 * 60 * 60
        os.utime(path, (timestamp, timestamp))
    return path


class LocalStorageInventoryTests(unittest.TestCase):
    def test_inventory_separates_removable_groups_from_sources_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            work = root / "work"
            data.mkdir()
            work.mkdir()
            _write(data / "cache" / "generated.bin", b"c" * 11)
            _write(data / "cache" / "final.mp4", b"protected-final")
            _write(data / "logs" / "job-error.log", b"l" * 13)
            _write(data / "updates" / "downloads" / "old.zip", b"u" * 17)
            _write(data / "production-inputs" / "novel.txt", b"source")
            _write(work / "job-1" / ".work" / "voice" / "part.wav", b"w" * 19)
            _write(work / "job-1" / ".previews" / "sample.mp4", b"p" * 23)
            _write(work / "job-1" / "final.mp4", b"final")

            report = inspect_local_storage(data, work)

            categories = report["categories"]
            self.assertEqual(categories["regenerable_cache"], {"files": 3, "bytes": 53})
            self.assertEqual(categories["diagnostic_logs"], {"files": 1, "bytes": 13})
            self.assertEqual(categories["update_rollback"], {"files": 1, "bytes": 17})
            self.assertEqual(categories["protected"]["files"], 3)
            self.assertEqual(report["removable"], {"files": 5, "bytes": 83})
            self.assertEqual(report["skipped_links"], 0)

    def test_cleanup_is_dry_run_until_confirm_is_exactly_true(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            data.mkdir()
            cache = _write(data / "cache" / "old.bin", b"cache", age_days=60)
            log = _write(data / "logs" / "job-error.log", b"error", age_days=60)
            update = _write(
                data / "updates" / "downloads" / "old.zip",
                b"update",
                age_days=60,
            )
            source = _write(data / "production-inputs" / "novel.txt", b"source", age_days=60)

            preview = cleanup_local_storage(
                data,
                confirm="yes",  # type: ignore[arg-type]
                min_age_days=30,
                max_delete_bytes=1024,
            )

            self.assertTrue(preview["dry_run"])
            self.assertEqual(preview["selected"]["files"], 3)
            self.assertTrue(cache.exists())
            self.assertTrue(log.exists())
            self.assertTrue(update.exists())

            applied = cleanup_local_storage(
                data,
                confirm=True,
                min_age_days=30,
                max_delete_bytes=1024,
            )

            self.assertFalse(applied["dry_run"])
            self.assertEqual(applied["deleted"]["files"], 3)
            self.assertFalse(cache.exists())
            self.assertFalse(log.exists())
            self.assertFalse(update.exists())
            self.assertTrue(source.exists())

    def test_cleanup_respects_age_and_byte_quota(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            data.mkdir()
            oldest = _write(data / "cache" / "oldest.bin", b"1" * 8, age_days=90)
            old = _write(data / "cache" / "old.bin", b"2" * 8, age_days=60)
            recent = _write(data / "cache" / "recent.bin", b"3" * 8, age_days=2)

            result = cleanup_local_storage(
                data,
                confirm=True,
                min_age_days=30,
                max_delete_bytes=8,
            )

            self.assertEqual(result["selected"], {
                "files": 1,
                "bytes": 8,
                "categories": {
                    "regenerable_cache": {"files": 1, "bytes": 8},
                    "diagnostic_logs": {"files": 0, "bytes": 0},
                    "update_rollback": {"files": 0, "bytes": 0},
                },
            })
            self.assertFalse(oldest.exists())
            self.assertTrue(old.exists())
            self.assertTrue(recent.exists())

    def test_pending_update_package_is_protected_from_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            data.mkdir()
            package = _write(
                data / "updates" / "downloads" / "pending.zip",
                b"pending-update",
                age_days=90,
            )
            marker = data / "updates" / "pending-update.json"
            marker.write_text(
                json.dumps({"package_path": str(package.resolve())}),
                encoding="utf-8",
            )

            report = inspect_local_storage(data)
            result = cleanup_local_storage(
                data,
                confirm=True,
                min_age_days=0,
                max_delete_bytes=1024,
            )

            self.assertGreaterEqual(report["categories"]["protected"]["files"], 2)
            self.assertTrue(package.exists())
            self.assertEqual(result["deleted"]["files"], 0)

    def test_root_and_symlink_roots_are_rejected(self) -> None:
        filesystem_root = Path(Path.cwd().anchor)
        with self.assertRaises(ValueError):
            inspect_local_storage(filesystem_root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            link = root / "linked"
            real.mkdir()
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError:
                with patch(
                    "storyforge.support_bundle._has_link_component",
                    return_value=True,
                ):
                    with self.assertRaises(ValueError):
                        cleanup_local_storage(real, confirm=True)
            else:
                with self.assertRaises(ValueError):
                    cleanup_local_storage(link, confirm=True)

    def test_internal_symlink_never_escapes_cleanup_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            outside = _write(root / "outside.txt", b"do-not-delete", age_days=90)
            cache = data / "cache"
            cache.mkdir(parents=True)
            link = cache / "escape.bin"
            try:
                link.symlink_to(outside)
            except OSError:
                link.write_bytes(b"simulated-link")

                def simulated_link(candidate: Path) -> bool:
                    return Path(candidate) == link

                with patch(
                    "storyforge.support_bundle._is_link_or_junction",
                    side_effect=simulated_link,
                ):
                    report = inspect_local_storage(data)
                    result = cleanup_local_storage(
                        data,
                        confirm=True,
                        min_age_days=0,
                        max_delete_bytes=1024,
                    )
            else:
                report = inspect_local_storage(data)
                result = cleanup_local_storage(
                    data,
                    confirm=True,
                    min_age_days=0,
                    max_delete_bytes=1024,
                )

            self.assertGreaterEqual(report["skipped_links"], 1)
            self.assertEqual(result["deleted"]["files"], 0)
            self.assertEqual(outside.read_bytes(), b"do-not-delete")


class SupportBundleTests(unittest.TestCase):
    def test_bundle_contains_only_allowlisted_sanitized_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            _write(
                data / "render-work" / "job-1" / "job-error.log",
                (
                    "Authorization: Bearer super-secret-token\n"
                    "api_key=employee-secret\n"
                    "source: C:\\Users\\Alice\\Private Story.txt\n"
                    "story: FULL NOVEL BODY SENTINEL\n"
                    "FFmpeg error while opening encoder\n"
                ).encode("utf-8"),
            )
            _write(data / "logs" / "ordinary.log", b"not an error log")
            _write(data / "render-work" / "job" / "final.mp4", b"video")
            destination = root / "support.zip"

            result = create_support_bundle(
                destination,
                data_dir=data,
                system_overview={
                    "platform": "Windows-Test",
                    "python": "3.12",
                    "machine": "AMD64",
                    "ffmpeg_ready": True,
                    "ffmpeg_path": r"C:\\Secret\\ffmpeg.exe",
                    "account": "Alice",
                },
                resource_status={
                    "memory": {
                        "total_bytes": 16 * 1024**3,
                        "available_bytes": 8 * 1024**3,
                        "low": False,
                    },
                    "output_path": r"D:\\Private Output",
                    "token": "another-secret",
                },
            )

            self.assertEqual(result, destination.resolve())
            with zipfile.ZipFile(result) as archive:
                self.assertEqual(
                    sorted(archive.namelist()),
                    ["logs/error-001.log", "summary.json"],
                )
                summary = json.loads(archive.read("summary.json"))
                combined = "\n".join(
                    archive.read(name).decode("utf-8")
                    for name in archive.namelist()
                )

            self.assertEqual(summary["app_version"], __version__)
            self.assertEqual(summary["included_error_logs"], 1)
            self.assertNotIn("ffmpeg_path", summary["system"])
            self.assertNotIn("account", summary["system"])
            self.assertNotIn("output_path", summary["resources"])
            self.assertNotIn("token", summary["resources"])
            self.assertNotIn("super-secret-token", combined)
            self.assertNotIn("employee-secret", combined)
            self.assertNotIn("Alice", combined)
            self.assertNotIn("FULL NOVEL BODY SENTINEL", combined)
            self.assertNotIn(r"C:\\Users", combined)
            self.assertIn("FFmpeg error while opening encoder", combined)

    def test_bundle_limits_error_log_count_and_tail_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            older = _write(
                data / "logs" / "first-error.log",
                b"old error",
                age_days=2,
            )
            newer = _write(
                data / "logs" / "second-error.log",
                b"X" * 4096 + b"\nFFmpeg error while opening encoder",
                age_days=1,
            )
            self.assertTrue(older.is_file() and newer.is_file())

            destination = root / "bounded.zip"
            create_support_bundle(
                destination,
                data_dir=data,
                max_log_files=1,
                log_tail_bytes=128,
            )

            with zipfile.ZipFile(destination) as archive:
                names = sorted(archive.namelist())
                log_text = archive.read("logs/error-001.log").decode("utf-8")
                summary = json.loads(archive.read("summary.json"))
            self.assertEqual(names, ["logs/error-001.log", "summary.json"])
            self.assertEqual(summary["included_error_logs"], 1)
            self.assertIn("FFmpeg error while opening encoder", log_text)
            self.assertLess(len(log_text.encode("utf-8")), 512)

    def test_bundle_drops_unprefixed_novel_prose_but_keeps_error_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            prose = (
                "UNPREFIXED NOVEL SENTINEL "
                "She opened the letter and discovered the secret her family "
                "had hidden for decades. "
            ) * 12
            _write(
                data / "logs" / "provider-error.log",
                (
                    prose
                    + "\nProviderResponseError: remote provider rejected the request\n"
                ).encode("utf-8"),
            )

            destination = root / "support.zip"
            create_support_bundle(destination, data_dir=data)

            with zipfile.ZipFile(destination) as archive:
                log_text = archive.read("logs/error-001.log").decode("utf-8")
            self.assertNotIn("UNPREFIXED NOVEL SENTINEL", log_text)
            self.assertNotIn("She opened the letter", log_text)
            self.assertIn("ProviderResponseError", log_text)

    def test_bundle_drops_json_request_body_prompt_and_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            request_body = json.dumps(
                {
                    "request": {
                        "body": {
                            "prompt": "JSON PROMPT SENTINEL",
                            "messages": [
                                {
                                    "role": "user",
                                    "content": "JSON NOVEL BODY SENTINEL",
                                }
                            ],
                        }
                    }
                },
                indent=2,
            )
            _write(
                data / "logs" / "ffmpeg-error.log",
                (
                    f"ProviderResponseError: provider request rejected\n{request_body}\n"
                    "FFmpeg error while opening encoder\n"
                ).encode("utf-8"),
            )

            destination = root / "support.zip"
            create_support_bundle(destination, data_dir=data)

            with zipfile.ZipFile(destination) as archive:
                log_text = archive.read("logs/error-001.log").decode("utf-8")
            self.assertNotIn("JSON PROMPT SENTINEL", log_text)
            self.assertNotIn("JSON NOVEL BODY SENTINEL", log_text)
            self.assertNotIn('"messages"', log_text)
            self.assertNotIn('"prompt"', log_text)
            self.assertIn("FFmpeg error while opening encoder", log_text)


class LocalStorageApiTests(unittest.TestCase):
    def _api(self, data_dir: Path) -> StoryForgeApi:
        api = StoryForgeApi.__new__(StoryForgeApi)
        api._repository = SimpleNamespace(data_dir=data_dir)
        return api

    def test_api_requires_exact_boolean_confirmation_before_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            data.mkdir()
            cache = _write(data / "cache" / "old.bin", b"cache", age_days=90)
            api = self._api(data)

            preview = api.cleanup_local_storage_cache(
                {
                    "confirm": "true",
                    "min_age_days": 30,
                    "max_delete_bytes": 1024,
                }
            )
            self.assertTrue(preview["ok"])
            self.assertTrue(preview["data"]["dry_run"])
            self.assertTrue(cache.exists())

            applied = api.cleanup_local_storage_cache(
                {
                    "confirm": True,
                    "min_age_days": 30,
                    "max_delete_bytes": 1024,
                }
            )
            self.assertTrue(applied["ok"])
            self.assertEqual(applied["data"]["deleted"]["files"], 1)
            self.assertFalse(cache.exists())

    def test_api_creates_support_bundle_under_local_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            data.mkdir()
            _write(data / "logs" / "render-error.log", b"encoder failed")
            api = self._api(data)

            response = api.create_local_support_bundle()

            self.assertTrue(response["ok"])
            created = Path(response["data"]["path"])
            self.assertTrue(created.is_file())
            self.assertEqual(created.parent, data / "support")


if __name__ == "__main__":
    unittest.main()
