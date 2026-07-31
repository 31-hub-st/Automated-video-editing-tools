from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storyforge import maintenance


class LocalCacheMaintenanceTests(unittest.TestCase):
    def test_render_work_cleanup_removes_only_reproducible_media(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "render-work"
            job = root / "batch-1" / "job-1"
            voice = job / ".work" / "voice" / "sentence.wav"
            narration = job / ".work" / "narration.wav"
            subtitle = job / ".work" / "subtitles.ass"
            preview = job / ".previews" / "legacy-sample.mp4"
            manifest = job / "manifest.json"
            error_log = job / "render-error.log"
            for path, payload in (
                (voice, b"voice"),
                (narration, b"narration"),
                (subtitle, b"subtitle"),
                (preview, b"preview"),
                (manifest, b"manifest"),
                (error_log, b"diagnostic"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)

            result = maintenance.prune_render_work_residuals(root)

            self.assertEqual(result["files"], 3)
            self.assertEqual(result["bytes"], len(b"voicenarrationpreview"))
            self.assertFalse(voice.exists())
            self.assertFalse(narration.exists())
            self.assertFalse(preview.exists())
            self.assertTrue(subtitle.exists())
            self.assertTrue(manifest.exists())
            self.assertTrue(error_log.exists())

    def test_voice_preview_cleanup_prunes_expired_then_oldest_to_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice-previews"
            now = 2_000_000_000.0
            expired = root / "novel-1" / "cache" / "old" / "old.wav"
            oldest = root / "novel-1" / "cache" / "a" / "preview.wav"
            newest = root / "novel-2" / "cache" / "b" / "preview.wav"
            for path in (expired, oldest, newest):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"123456")
                (path.parent / "preview.json").write_bytes(b"{}")
            os.utime(expired, (now - 20 * 86400, now - 20 * 86400))
            os.utime(
                expired.parent / "preview.json",
                (now - 20 * 86400, now - 20 * 86400),
            )
            os.utime(oldest, (now - 60, now - 60))
            os.utime(
                oldest.parent / "preview.json",
                (now - 60, now - 60),
            )
            os.utime(newest, (now - 30, now - 30))
            os.utime(
                newest.parent / "preview.json",
                (now - 30, now - 30),
            )

            with patch("storyforge.maintenance.time.time", return_value=now):
                result = maintenance.prune_voice_preview_cache(
                    root,
                    max_age_days=14,
                    max_bytes=8,
                )

            self.assertEqual(result, {"files": 4, "bytes": 16})
            self.assertFalse(expired.exists())
            self.assertFalse((expired.parent / "preview.json").exists())
            self.assertFalse(oldest.exists())
            self.assertFalse((oldest.parent / "preview.json").exists())
            self.assertTrue(newest.exists())
            self.assertTrue((newest.parent / "preview.json").exists())

    def test_voice_preview_cleanup_keeps_protected_audio_and_sidecar_as_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "voice-previews"
            protected_dir = root / "novel-1" / "cache" / "current"
            old_dir = root / "novel-1" / "cache" / "old"
            protected_audio = protected_dir / "candidate.wav"
            protected_sidecar = protected_dir / "preview.json"
            old_audio = old_dir / "candidate.wav"
            old_sidecar = old_dir / "preview.json"
            for path in (
                protected_audio,
                protected_sidecar,
                old_audio,
                old_sidecar,
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"123456")

            result = maintenance.prune_voice_preview_cache(
                root,
                max_age_days=0,
                max_bytes=0,
                protected_paths=(protected_audio, protected_sidecar),
            )

            self.assertEqual(result, {"files": 2, "bytes": 12})
            self.assertTrue(protected_audio.is_file())
            self.assertTrue(protected_sidecar.is_file())
            self.assertFalse(old_audio.exists())
            self.assertFalse(old_sidecar.exists())

    def test_startup_maintenance_runs_each_data_dir_once_and_tts_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            with (
                patch.object(maintenance, "_MAINTAINED_DATA_DIRS", set()),
                patch.object(maintenance, "_TTS_CACHE_MAINTAINED", False),
                patch.object(
                    maintenance,
                    "prune_render_work_residuals",
                    return_value={"files": 0, "bytes": 0},
                ) as render_prune,
                patch.object(
                    maintenance,
                    "prune_voice_preview_cache",
                    return_value={"files": 0, "bytes": 0},
                ) as preview_prune,
                patch.object(maintenance, "prune_tts_cache", return_value=4) as tts_prune,
            ):
                one = maintenance.run_startup_cache_maintenance(first)
                repeated = maintenance.run_startup_cache_maintenance(first)
                two = maintenance.run_startup_cache_maintenance(second)

            self.assertFalse(one["skipped"])
            self.assertTrue(repeated["skipped"])
            self.assertFalse(two["skipped"])
            self.assertEqual(render_prune.call_count, 2)
            self.assertEqual(preview_prune.call_count, 2)
            self.assertEqual(tts_prune.call_count, 1)


if __name__ == "__main__":
    unittest.main()
