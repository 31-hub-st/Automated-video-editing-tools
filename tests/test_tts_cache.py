from __future__ import annotations

import io
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from storyforge.providers.base import ProviderConfig
from storyforge.providers.tts import TTSProvider, wav_duration


def wav_bytes(duration: float = 0.1, rate: int = 16_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x00\x00" * int(rate * duration))
    return output.getvalue()


class CountingProvider(TTSProvider):
    def __init__(
        self,
        config: ProviderConfig,
        *,
        audio: bytes | None = None,
        direct_output: bool = False,
        failure: BaseException | None = None,
    ) -> None:
        super().__init__(config)
        self.audio = audio or wav_bytes(0.05)
        self.direct_output = direct_output
        self.failure = failure
        self.calls = 0

    def _generate_audio(
        self, text: str, voice: str, speed: float, output_path: Path
    ) -> bytes | None:
        del text, voice, speed
        self.calls += 1
        if self.failure is not None:
            # Simulate an external CLI leaving an incomplete result before it
            # reports an error.  The public output and cache must remain clean.
            output_path.write_bytes(b"partial wav")
            raise self.failure
        if self.direct_output:
            output_path.write_bytes(self.audio)
            return None
        return self.audio


def config(
    cache_dir: Path,
    *,
    name: str = "fake-local",
    model: str = "model-one",
    lang: str = "en",
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        model=model,
        options={"cache_dir": cache_dir, "lang_code": lang},
    )


class SentenceTTSCacheTests(unittest.TestCase):
    def test_identical_sentence_is_reused_across_providers_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache_dir = root / "cache"
            first = CountingProvider(config(cache_dir))
            first_result = first.synthesize_sentence(
                "The phone rang.", root / "first.wav", voice="woman", speed=1.2
            )
            second = CountingProvider(config(cache_dir))
            second_result = second.synthesize_sentence(
                "The phone rang.", root / "second.wav", voice="woman", speed=1.2
            )

            self.assertEqual(first.calls, 1)
            self.assertEqual(second.calls, 0)
            self.assertEqual(Path(first_result.path).read_bytes(), Path(second_result.path).read_bytes())
            self.assertGreater(second_result.duration_seconds, 0)
            cached = list(cache_dir.rglob("*.wav"))
            self.assertEqual(len(cached), 1)
            self.assertIn("storyforge-tts-wav-v1", cached[0].parts)

    def test_cache_identity_changes_for_every_voice_input(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache_dir = root / "cache"
            baseline = CountingProvider(config(cache_dir))
            baseline.synthesize_sentence(
                "Same sentence.", root / "base.wav", voice="v1", speed=1.0
            )
            identical = CountingProvider(config(cache_dir))
            identical.synthesize_sentence(
                "Same sentence.", root / "same.wav", voice="v1", speed=1.0
            )
            self.assertEqual(identical.calls, 0)

            variants = [
                (config(cache_dir, name="another-provider"), "Same sentence.", "v1", 1.0),
                (config(cache_dir, model="model-two"), "Same sentence.", "v1", 1.0),
                (config(cache_dir), "Same sentence.", "v2", 1.0),
                (config(cache_dir), "Same sentence.", "v1", 1.1),
                (config(cache_dir), "Different sentence.", "v1", 1.0),
                (config(cache_dir, lang="fr"), "Same sentence.", "v1", 1.0),
            ]
            for index, (provider_config, text, voice, speed) in enumerate(variants):
                provider = CountingProvider(provider_config)
                provider.synthesize_sentence(
                    text, root / f"variant-{index}.wav", voice=voice, speed=speed
                )
                self.assertEqual(provider.calls, 1)

            with patch(
                "storyforge.providers.tts._TTS_CACHE_SCHEMA",
                "storyforge-tts-wav-test-v2",
            ):
                changed_schema = CountingProvider(config(cache_dir))
                changed_schema.synthesize_sentence(
                    "Same sentence.", root / "schema.wav", voice="v1", speed=1.0
                )
                self.assertEqual(changed_schema.calls, 1)

    def test_corrupt_cache_is_rejected_and_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache_dir = root / "cache"
            first = CountingProvider(config(cache_dir))
            first.synthesize_sentence("Repair me.", root / "first.wav", voice="v1")
            cache_path = next(cache_dir.rglob("*.wav"))
            cache_path.write_bytes(b"not a wav")

            replacement = CountingProvider(config(cache_dir))
            replacement.synthesize_sentence(
                "Repair me.", root / "replacement.wav", voice="v1"
            )

            self.assertEqual(replacement.calls, 1)
            self.assertGreater(wav_duration(cache_path), 0)
            self.assertEqual(list(cache_dir.rglob("*.tmp")), [])

    def test_generation_failure_leaves_output_and_cache_unpolluted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache_dir = root / "cache"
            output = root / "voice.wav"
            previous = wav_bytes(0.02)
            output.write_bytes(previous)
            failing = CountingProvider(
                config(cache_dir), failure=RuntimeError("synthetic engine failure")
            )

            with self.assertRaisesRegex(RuntimeError, "synthetic engine failure"):
                failing.synthesize_sentence("Retry me.", output, voice="v1")

            self.assertEqual(output.read_bytes(), previous)
            self.assertEqual(list(cache_dir.rglob("*.wav")), [])
            self.assertEqual(list(root.glob(f".{output.stem}-*.tmp.wav")), [])

            retry = CountingProvider(config(cache_dir))
            retry.synthesize_sentence("Retry me.", output, voice="v1")
            self.assertEqual(retry.calls, 1)
            self.assertEqual(len(list(cache_dir.rglob("*.wav"))), 1)

    def test_direct_output_provider_is_cached_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache_dir = root / "cache"
            first = CountingProvider(config(cache_dir), direct_output=True)
            first.synthesize_sentence("CLI output.", root / "first.wav", voice="v1")
            second = CountingProvider(config(cache_dir), direct_output=True)
            second.synthesize_sentence("CLI output.", root / "second.wav", voice="v1")
            self.assertEqual(first.calls, 1)
            self.assertEqual(second.calls, 0)

    def test_default_cache_uses_local_appdata(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            environment = {
                "LOCALAPPDATA": str(root / "LocalAppData"),
                "STORYFORGE_TTS_CACHE_DIR": "",
            }
            with patch.dict(os.environ, environment, clear=False):
                provider = CountingProvider(
                    ProviderConfig(
                        name="fake-local",
                        model="model-one",
                        options={"lang_code": "en"},
                    )
                )
                provider.synthesize_sentence("Default path.", root / "voice.wav")

            expected = root / "LocalAppData" / "StoryForgeStudio" / "cache" / "tts"
            self.assertEqual(len(list(expected.rglob("*.wav"))), 1)


if __name__ == "__main__":
    unittest.main()
