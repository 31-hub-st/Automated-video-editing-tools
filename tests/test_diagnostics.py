from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from storyforge.diagnostics import run_kokoro_self_test
from storyforge.main import build_parser
from storyforge.providers.base import ProviderConfig


class DiagnosticsTests(unittest.TestCase):
    def test_parser_accepts_packaged_kokoro_self_test_directory(self) -> None:
        args = build_parser().parse_args(["--kokoro-self-test", "C:/smoke"])
        self.assertEqual(args.kokoro_self_test, "C:/smoke")

    def test_kokoro_self_test_writes_verified_wav_and_json(self) -> None:
        class FakeProvider:
            def synthesize(self, _sentences, output_dir, **_kwargs):
                wav_path = Path(output_dir) / "kokoro-self-test-0001.wav"
                with wave.open(str(wav_path), "wb") as stream:
                    stream.setnchannels(1)
                    stream.setsampwidth(2)
                    stream.setframerate(24_000)
                    stream.writeframes(b"\x00\x00" * 2_400)
                return SimpleNamespace(
                    path=str(wav_path),
                    provider="local_kokoro",
                    model="kokoro",
                    duration_seconds=0.1,
                )

        def factory(config):
            self.assertIsInstance(config, ProviderConfig)
            self.assertEqual(config.name, "local_kokoro")
            self.assertIs(config.options.get("cache_enabled"), False)
            return FakeProvider()

        with tempfile.TemporaryDirectory() as folder:
            code = run_kokoro_self_test(folder, provider_factory=factory)
            payload = json.loads(
                Path(folder, "kokoro-self-test.json").read_text(encoding="utf-8")
            )
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["sample_rate"], 24_000)
            self.assertEqual(payload["frame_count"], 2_400)
            self.assertTrue(payload["tts_cache_bypassed"])
            self.assertTrue(Path(payload["wav_path"]).is_file())

    def test_kokoro_self_test_persists_failure_details(self) -> None:
        class FailingProvider:
            def synthesize(self, *_args, **_kwargs):
                raise FileNotFoundError("missing packaged data")

        with tempfile.TemporaryDirectory() as folder:
            code = run_kokoro_self_test(
                folder,
                provider_factory=lambda _config: FailingProvider(),
            )
            payload = json.loads(
                Path(folder, "kokoro-self-test.json").read_text(encoding="utf-8")
            )
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error_type"], "FileNotFoundError")
            self.assertIn("missing packaged data", payload["traceback"])


if __name__ == "__main__":
    unittest.main()
