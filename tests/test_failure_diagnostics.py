from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from storyforge.failure_diagnostics import (
    DIAGNOSTIC_SCHEMA_VERSION,
    capture_failure_diagnostics,
    classify_failure,
    sanitize_failure_log,
)


class FailureDiagnosticsTests(unittest.TestCase):
    def test_classifies_common_ffmpeg_failures(self) -> None:
        cases = {
            "missing_input": "Error opening input: No such file or directory",
            "corrupt_media": "moov atom not found; Invalid data found when processing input",
            "permission_denied": "Could not write header: Permission denied",
            "disk_full": "av_interleaved_write_frame(): No space left on device",
            "filter_or_subtitle": "Error initializing complex filters. Failed to configure output pad",
            "encoder_init": "Error while opening encoder for output stream #0:0",
            "out_of_memory": "av_malloc: Cannot allocate memory",
            "unknown": "Conversion failed for an unexpected reason",
        }
        for expected, log_text in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(classify_failure(log_text), expected)

    def test_sanitizes_controls_paths_and_secrets(self) -> None:
        raw = (
            "\x1b[31mFAILED\x1b[0m\x00\t"
            'input="C:\\Users\\Alice Smith\\Private\\story.mp4" '
            "cache=\\\\studio-pc\\renders\\secret\\clip.wav\n"
            "font=/home/alice/private/My Font.ttf\n"
            "cache=/data/Worker Name/private folder\n"
            "source=file:///Users/alice/render/input.mov\n"
            "Authorization: Bearer super-secret-token\n"
            "token=abc123 password: p@ss api-key='key-value'\n"
            "https://alice:private@example.test/render?token=url-token&quality=high"
        )
        safe = sanitize_failure_log(raw)

        self.assertIn("FAILED", safe)
        self.assertNotIn("\x1b", safe)
        self.assertNotIn("\x00", safe)
        for secret in (
            "Alice Smith",
            "studio-pc",
            "/home/alice",
            "/data/Worker Name",
            "/Users/alice",
            "super-secret-token",
            "abc123",
            "p@ss",
            "key-value",
            "alice:private",
            "url-token",
        ):
            self.assertNotIn(secret, safe)
        self.assertIn("<path>", safe)
        self.assertIn("Bearer <redacted>", safe)
        self.assertIn("quality=high", safe)

    def test_capture_reads_bounded_tail_and_returns_json_value(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            log_path = Path(folder, "render-error.log")
            log_path.write_text(
                ("PRIVATE-PREFIX\n" * 200)
                + "C:\\Users\\Worker\\input.mp4: Permission denied\n"
                + ("tail-detail\n" * 50),
                encoding="utf-8",
            )
            report = capture_failure_diagnostics(
                log_path,
                stage="ffmpeg_render",
                max_read_bytes=800,
                max_log_chars=240,
            )

        self.assertEqual(report["schema_version"], DIAGNOSTIC_SCHEMA_VERSION)
        self.assertEqual(report["code"], "permission_denied")
        self.assertEqual(report["stage"], "ffmpeg_render")
        self.assertEqual(report["log_name"], "render-error.log")
        self.assertTrue(report["truncated"])
        self.assertLessEqual(len(str(report["log_tail"])), 240)
        self.assertNotIn("Worker", str(report["log_tail"]))
        self.assertNotIn("PRIVATE-PREFIX", str(report["log_tail"]))
        self.assertIsInstance(json.dumps(report, ensure_ascii=False), str)

    def test_default_capture_never_exceeds_six_thousand_characters(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            log_path = Path(folder, "render-error.log")
            log_path.write_text("x" * 20_000, encoding="utf-8")
            report = capture_failure_diagnostics(log_path)

        self.assertEqual(len(str(report["log_tail"])), 6_000)
        self.assertTrue(report["truncated"])

    def test_missing_log_returns_safe_unknown_report(self) -> None:
        report = capture_failure_diagnostics(
            Path("C:/Users/Private Person/missing-render-error.log")
        )

        self.assertEqual(report["code"], "unknown")
        self.assertEqual(report["log_name"], "missing-render-error.log")
        self.assertEqual(report["log_tail"], "")
        self.assertFalse(report["truncated"])
        self.assertNotIn("Private Person", json.dumps(report, ensure_ascii=False))

    def test_invalid_utf8_and_bidi_controls_do_not_break_capture(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            log_path = Path(folder, "render-error.log")
            log_path.write_bytes(
                b"Unknown failure\xff\xfe\n" + "hidden\u202epath".encode("utf-8")
            )
            report = capture_failure_diagnostics(log_path)

        self.assertEqual(report["code"], "unknown")
        self.assertNotIn("\u202e", str(report["log_tail"]))
        self.assertIn("Unknown failure", str(report["log_tail"]))


if __name__ == "__main__":
    unittest.main()
