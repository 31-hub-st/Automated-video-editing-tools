from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path

from storyforge.models import AppSettings
from storyforge.providers.base import ProviderConfigurationError
from storyforge.providers.tts import SpeechSegment, TTSResult
from storyforge.services.voice_preview import VoicePreviewService, audition_excerpt


def _write_wav(path: Path, seconds: float = 0.05) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * max(1, round(16000 * seconds)))


class _FakeProvider:
    def __init__(self) -> None:
        self.voices: list[str] = []

    def synthesize(self, texts, output_dir, *, voice, speed, file_stem):
        self.voices.append(voice)
        path = Path(output_dir) / f"{file_stem}.wav"
        _write_wav(path)
        return TTSResult(
            provider="fake",
            segments=(
                SpeechSegment(
                    index=0,
                    text=texts[0],
                    path=str(path),
                    duration_seconds=0.05,
                    voice=voice,
                    provider="fake",
                ),
            ),
        )


class _TimedFakeProvider(_FakeProvider):
    def synthesize(self, texts, output_dir, *, voice, speed, file_stem):
        self.voices.append(voice)
        duration = len(texts[0].split()) * 60.0 / 240.0
        path = Path(output_dir) / f"{file_stem}.wav"
        _write_wav(path, seconds=duration)
        return TTSResult(
            provider="fake",
            segments=(
                SpeechSegment(
                    index=0,
                    text=texts[0],
                    path=str(path),
                    duration_seconds=duration,
                    voice=voice,
                    provider="fake",
                ),
            ),
        )


class VoicePreviewTests(unittest.TestCase):
    def test_excerpt_uses_real_body_and_hides_chapter_heading(self) -> None:
        excerpt = audition_excerpt(
            "Chapter 1\nThe phone rang at ten. She whispered my husband's name."
        )
        self.assertNotIn("Chapter 1", excerpt)
        self.assertIn("husband's name", excerpt)

    def test_japanese_excerpt_hides_heading_and_honors_native_punctuation(self) -> None:
        excerpt = audition_excerpt(
            "第1話 真夜中の電話\n電話が鳴った。女は夫の名前を囁いた！私は扉を開けなかった。",
            language="ja",
        )
        self.assertNotIn("第1話", excerpt)
        self.assertNotIn("真夜中の電話", excerpt)
        self.assertIn("電話が鳴った。", excerpt)
        self.assertIn("女は夫の名前を囁いた！", excerpt)

    def test_hindi_excerpt_honors_danda_and_never_sends_the_whole_story(self) -> None:
        sentence = "उसने दरवाज़ा खोला और अपने पति का छिपा हुआ सच देखा।"
        story = " ".join(sentence for _ in range(20))
        excerpt = audition_excerpt(
            story,
            language="hi",
            maximum_words=42,
        )
        self.assertLessEqual(len(excerpt.split()), 42)
        self.assertLess(len(excerpt), len(story))
        self.assertIn("।", excerpt)
        complete_excerpt = excerpt.removesuffix("…")
        self.assertEqual(complete_excerpt.split(), story.split()[:42])
        self.assertEqual(complete_excerpt.split()[-1], story.split()[41])

    def test_hindi_excerpt_never_cuts_inside_a_combining_mark_word(self) -> None:
        excerpt = audition_excerpt(
            "दरवाज़ा खुला लेकिन उसके पीछे एक और सच था",
            language="hi",
            maximum_words=1,
        )
        self.assertEqual(excerpt, "दरवाज़ा…")

    def test_hindi_long_line_without_danda_still_keeps_complete_words(self) -> None:
        words = ["दरवाज़ा", "खुला", "लेकिन", "राज़", "बाकी"] * 12
        excerpt = audition_excerpt(
            " ".join(words), language="hi", maximum_words=42
        )
        self.assertEqual(excerpt.removesuffix("…").split(), words[:42])

    def test_latin_excerpt_preserves_apostrophe_and_hyphen_tokens(self) -> None:
        words = ["her", "husband's", "late-night", "secret"] * 15
        story = " ".join(words)
        excerpt = audition_excerpt(story, language="en", maximum_words=42)
        self.assertEqual(excerpt.removesuffix("…").split(), words[:42])

    def test_japanese_long_sentence_honors_character_limit(self) -> None:
        story = "第2話\n" + ("彼女は隠された扉の向こうに真実を見つけた" * 20)
        excerpt = audition_excerpt(
            story, language="ja", maximum_characters=180
        )
        self.assertNotIn("第2話", excerpt)
        self.assertLessEqual(len(excerpt.removesuffix("…")), 180)
        self.assertLess(len(excerpt), len(story))

    def test_generates_three_distinct_candidate_voice_ids(self) -> None:
        settings = AppSettings()
        fake = _FakeProvider()
        with tempfile.TemporaryDirectory() as temporary:
            service = VoicePreviewService(
                lambda: settings,
                tts_provider_factory=lambda _config: fake,
            )
            result = service.generate(
                "The call came at ten. I answered, and a woman said my husband's name.",
                "suspense",
                temporary,
            )
        self.assertEqual(len(result), 3)
        self.assertEqual(len({item["voice_id"] for item in result}), 3)
        self.assertTrue(all(item["audio_uri"].startswith("file:") for item in result))

    def test_given_wpm_generates_real_eight_to_twelve_second_cached_previews(self) -> None:
        settings = AppSettings()
        fake = _TimedFakeProvider()
        story = " ".join(
            f"word{index}{'.' if index % 12 == 0 else ''}"
            for index in range(1, 201)
        )
        with tempfile.TemporaryDirectory() as temporary:
            service = VoicePreviewService(
                lambda: settings,
                tts_provider_factory=lambda _config: fake,
            )
            first = service.generate(
                story,
                "suspense",
                temporary,
                narration_wpm=240,
            )
            second = service.generate(
                story,
                "suspense",
                temporary,
                narration_wpm=240,
            )

        self.assertEqual(len(fake.voices), 3)
        self.assertTrue(all(8.0 <= item["duration_seconds"] <= 12.0 for item in first))
        self.assertTrue(all(item["narration_wpm"] == 240 for item in first))
        self.assertTrue(all(not item["cached"] for item in first))
        self.assertTrue(all(item["cached"] for item in second))
        self.assertEqual(
            [item["cache_key"] for item in first],
            [item["cache_key"] for item in second],
        )

    def test_preview_wpm_rejects_values_outside_supported_custom_range(self) -> None:
        settings = AppSettings()
        with tempfile.TemporaryDirectory() as temporary:
            service = VoicePreviewService(
                lambda: settings,
                tts_provider_factory=lambda _config: _FakeProvider(),
            )
            for invalid in (199, 281, 240.5):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    service.generate(
                        "A sufficiently long story sentence for preview generation.",
                        "suspense",
                        temporary,
                        narration_wpm=invalid,
                    )

    def test_japanese_local_preview_routes_kokoro_language_and_real_female_ids(self) -> None:
        settings = AppSettings()
        fake = _FakeProvider()
        configs = []

        def factory(config):
            configs.append(config)
            return fake

        with tempfile.TemporaryDirectory() as temporary:
            service = VoicePreviewService(
                lambda: settings,
                tts_provider_factory=factory,
            )
            result = service.generate(
                "電話が鳴った。女は夫の名前を囁いた。私は凍りついた。",
                "suspense",
                temporary,
                language="ja",
            )
        self.assertEqual(configs[0].options["lang_code"], "j")
        self.assertEqual(
            [item["voice_id"] for item in result],
            ["jf_alpha", "jf_gongitsune", "jf_nezumi"],
        )
        self.assertTrue(all(item["language"] == "ja" for item in result))

    def test_single_voice_language_returns_one_real_candidate_not_fake_ids(self) -> None:
        settings = AppSettings()
        fake = _FakeProvider()
        with tempfile.TemporaryDirectory() as temporary:
            service = VoicePreviewService(
                lambda: settings,
                tts_provider_factory=lambda _config: fake,
            )
            result = service.generate(
                "Anoche sonó el teléfono. Una mujer dijo el nombre de mi marido.",
                "romance",
                temporary,
                language="es",
            )
        self.assertEqual([item["voice_id"] for item in result], ["ef_dora"])

    def test_unsupported_local_language_has_actionable_service_error(self) -> None:
        settings = AppSettings()
        with tempfile.TemporaryDirectory() as temporary:
            service = VoicePreviewService(lambda: settings)
            with self.assertRaisesRegex(
                ProviderConfigurationError,
                "尚未配置语种",
            ):
                service.generate(
                    "Suara telepon berdering pada tengah malam.",
                    "suspense",
                    temporary,
                    language="id",
                )


if __name__ == "__main__":
    unittest.main()
