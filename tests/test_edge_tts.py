from __future__ import annotations

import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from storyforge.api import StoryForgeApi
from storyforge.config import SettingsRepository
from storyforge.jobs import JobQueue
from storyforge.models import AppSettings
from storyforge.providers.base import ProviderConfig, ProviderConfigurationError
from storyforge.providers.tts import (
    EdgeTTSProvider,
    TTSVoiceOption,
    clear_edge_voice_cache,
    create_tts_provider,
    edge_female_voice_candidates,
    female_voice_candidates,
    normalize_tts_language,
)
from storyforge.services.voice_preview import VoicePreviewService


def _write_wav(path: Path, seconds: float = 0.05) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\0\0" * max(1, round(24000 * seconds)))


class _FakePreviewProvider:
    def synthesize(self, texts, output_dir, *, voice, speed, file_stem):
        from storyforge.providers.tts import SpeechSegment, TTSResult

        path = Path(output_dir) / f"{file_stem}.wav"
        _write_wav(path)
        return TTSResult(
            provider="edge_tts",
            segments=(
                SpeechSegment(
                    index=1,
                    text=texts[0],
                    path=str(path),
                    duration_seconds=0.05,
                    voice=voice,
                    provider="edge_tts",
                ),
            ),
        )


class EdgeVoiceCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_edge_voice_cache()

    def tearDown(self) -> None:
        clear_edge_voice_cache()

    def test_new_language_aliases_are_normalized_without_expanding_kokoro(self) -> None:
        self.assertEqual(normalize_tts_language("de-DE"), "de")
        self.assertEqual(normalize_tts_language("Bahasa Indonesia"), "id")
        self.assertEqual(normalize_tts_language("ko-KR"), "ko")
        self.assertEqual(female_voice_candidates("local_kokoro", "de"), ())

    def test_dynamic_catalog_returns_only_real_upstream_female_voices_up_to_three(self) -> None:
        locale_by_language = {
            "en": "en-US",
            "ja": "ja-JP",
            "es": "es-ES",
            "fr": "fr-FR",
            "de": "de-DE",
            "id": "id-ID",
            "ko": "ko-KR",
            "it": "it-IT",
            "pt-BR": "pt-BR",
            "hi": "hi-IN",
        }

        def upstream(*, proxy="", timeout_seconds=15.0):
            del proxy, timeout_seconds
            rows = []
            for locale in set(locale_by_language.values()):
                for name in ("Ada", "Bea", "Cora", "Dora"):
                    rows.append(
                        {
                            "ShortName": f"{locale}-{name}Neural",
                            "LocalName": name,
                            "Locale": locale,
                            "Gender": "Female",
                        }
                    )
                rows.append(
                    {
                        "ShortName": f"{locale}-MaleNeural",
                        "LocalName": "Male",
                        "Locale": locale,
                        "Gender": "Male",
                    }
                )
            return rows

        with (
            patch(
                "storyforge.providers.tts.edge_tts_runtime_available",
                return_value=True,
            ),
            patch("storyforge.providers.tts._query_edge_voices", side_effect=upstream),
        ):
            for language, locale in locale_by_language.items():
                with self.subTest(language=language):
                    voices = edge_female_voice_candidates(language, refresh=True)
                    self.assertEqual(len(voices), 3)
                    self.assertTrue(
                        all(item.voice_id.startswith(locale + "-") for item in voices)
                    )
                    self.assertEqual(len({item.voice_id for item in voices}), 3)

    def test_missing_component_never_returns_static_or_invented_candidates(self) -> None:
        with (
            patch(
                "storyforge.providers.tts.edge_tts_runtime_available",
                return_value=False,
            ),
            patch("storyforge.providers.tts._query_edge_voices") as query,
        ):
            self.assertEqual(edge_female_voice_candidates("de", refresh=True), ())
            self.assertEqual(female_voice_candidates("edge_tts", "ko"), ())
        query.assert_not_called()

    def test_provider_creation_reports_missing_optional_runtime(self) -> None:
        with patch(
            "storyforge.providers.tts.edge_tts_runtime_available",
            return_value=False,
        ):
            with self.assertRaisesRegex(ProviderConfigurationError, "pip install"):
                create_tts_provider(ProviderConfig(name="edge_tts"))

    def test_queue_readiness_uses_runtime_probe_and_never_requires_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            api = StoryForgeApi(
                repository=SettingsRepository(Path(temporary)),
                queue=JobQueue(lambda *_args: ""),
            )
            api._state.settings.providers.tts_provider = "edge_tts"
            api._state.settings.providers.tts_api_key = ""
            with (
                patch("storyforge.api.edge_tts_runtime_available", return_value=False),
                self.assertRaisesRegex(ValueError, "未安装 Edge TTS"),
            ):
                api._validate_provider_readiness()
            with patch(
                "storyforge.api.edge_tts_runtime_available", return_value=True
            ):
                api._validate_provider_readiness()


class EdgeProviderTests(unittest.TestCase):
    def test_provider_converts_downloaded_audio_to_valid_wav(self) -> None:
        class Communicate:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def save(self, path: str) -> None:
                Path(path).write_bytes(b"synthetic-mp3")

        fake_edge = SimpleNamespace(Communicate=Communicate)
        commands = []

        def runner(command, **kwargs):
            del kwargs
            commands.append(list(command))
            _write_wav(Path(command[-1]))
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ffmpeg = root / "ffmpeg.exe"
            ffmpeg.write_bytes(b"test executable marker")
            with (
                patch(
                    "storyforge.providers.tts.edge_tts_runtime_available",
                    return_value=True,
                ),
                patch.dict(sys.modules, {"edge_tts": fake_edge}),
                patch(
                    "storyforge.providers.tts._edge_cli_command",
                    return_value=["edge-tts-test"],
                ),
            ):
                provider = EdgeTTSProvider(
                    ProviderConfig(
                        name="edge_tts",
                        options={"cache_enabled": False, "language": "de"},
                    ),
                    runner=runner,
                    ffmpeg_executable=ffmpeg,
                )
                result = provider.synthesize(
                    ["In dieser Nacht klingelte das Telefon."],
                    root / "out",
                    voice="de-DE-AdaNeural",
                    speed=1.1,
                )
            self.assertTrue(Path(result.path).is_file())
            self.assertGreater(result.duration_seconds, 0)
            self.assertEqual(result.segments[0].voice, "de-DE-AdaNeural")
            self.assertEqual(commands[0][0], "edge-tts-test")
            self.assertIn("--file", commands[0])
            self.assertNotIn("In dieser Nacht klingelte das Telefon.", commands[0])

    def test_edge_audition_uses_current_story_text_not_fixed_english(self) -> None:
        settings = AppSettings()
        settings.providers.tts_provider = "edge_tts"
        catalog = (
            TTSVoiceOption("de-DE-AdaNeural", "Ada", "dramatic"),
            TTSVoiceOption("de-DE-BeaNeural", "Bea", "warm"),
            TTSVoiceOption("de-DE-CoraNeural", "Cora", "calm"),
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "storyforge.services.voice_preview.female_voice_candidates",
            return_value=catalog,
        ):
            result = VoicePreviewService(
                lambda: settings,
                tts_provider_factory=lambda _config: _FakePreviewProvider(),
            ).generate(
                "Kapitel 1\nIn dieser Nacht klingelte das Telefon. "
                "Eine Frau flüsterte den Namen meines Mannes.",
                "suspense",
                temporary,
                language="de-DE",
            )
        self.assertEqual(len(result), 3)
        self.assertTrue(all("Telefon" in item["excerpt"] for item in result))
        self.assertTrue(all("I thought it was a prank" not in item["excerpt"] for item in result))


if __name__ == "__main__":
    unittest.main()
