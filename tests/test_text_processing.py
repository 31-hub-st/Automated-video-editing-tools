from __future__ import annotations

import math
from pathlib import Path
import unittest

from storyforge.services.text_processing import (
    StoryFilenameError,
    analyze_manuscript,
    calculate_text_statistics,
    count_words,
    estimate_duration_seconds,
    extract_chapters,
    normalize_manuscript_text,
    normalize_typography,
    parse_story_filename,
    repair_mojibake,
    split_english_sentences,
)


class StoryFilenameTests(unittest.TestCase):
    def test_parses_code_and_title_from_full_path(self) -> None:
        parsed = parse_story_filename(Path("input") / "123456_Story Title.txt")

        self.assertEqual(parsed.code, "123456")
        self.assertEqual(parsed.title, "Story Title")
        self.assertEqual(parsed.filename, "123456_Story Title.txt")

    def test_parses_alphanumeric_code_and_preserves_case(self) -> None:
        parsed = parse_story_filename("B73165_GoodNovel.txt")

        self.assertEqual(parsed.code, "B73165")
        self.assertEqual(parsed.title, "GoodNovel")
        self.assertEqual(parsed.filename, "B73165_GoodNovel.txt")

    def test_only_first_underscore_is_structural(self) -> None:
        parsed = parse_story_filename("007_A_Second_Chance.TXT")
        self.assertEqual(parsed.code, "007")
        self.assertEqual(parsed.title, "A_Second_Chance")

    def test_rejects_malformed_names_with_actionable_error(self) -> None:
        invalid_names = (
            "Story Title.txt",
            "_Story Title.txt",
            "ABC-123_Story Title.txt",
            "123456_.txt",
            "123456_Story Title.docx",
        )

        for name in invalid_names:
            with self.subTest(name=name), self.assertRaisesRegex(
                StoryFilenameError, r"B73165_Story Title\.txt"
            ):
                parse_story_filename(name)


class MojibakeAndTypographyTests(unittest.TestCase):
    def test_repairs_gbk_rendering_observed_in_sample(self) -> None:
        # These escapes spell the visibly broken forms husband鈥檚, 鈥攁 and 鈥攈
        # without depending on the terminal's current Windows code page.
        broken = (
            "my husband\u9225\u6a9a name; "
            "bodies intertwined\u9225\u6501 sight; "
            "his face appeared\u9225\u6508e was angry"
        )

        repaired = normalize_manuscript_text(broken)

        self.assertEqual(
            repaired,
            "my husband's name; bodies intertwined\u2014a sight; "
            "his face appeared\u2014he was angry",
        )

    def test_repairs_windows_1252_mojibake(self) -> None:
        broken = "husband\u00e2\u20ac\u2122s secret\u00e2\u20ac\u201dwas out"
        self.assertEqual(
            normalize_manuscript_text(broken),
            "husband's secret\u2014was out",
        )

    def test_normalizes_mixed_smart_quotes_and_dash_spacing(self) -> None:
        sample_style = "\u201cDon't go,\u201d she said \u2013 then left. \u2018Max Jennings'"
        self.assertEqual(
            normalize_typography(sample_style),
            '\"Don\'t go,\" she said\u2014then left. \'Max Jennings\'',
        )

    def test_repair_function_preserves_already_correct_unicode(self) -> None:
        correct = "husband\u2019s name\u2014a secret"
        self.assertEqual(repair_mojibake(correct), correct)


class ChapterExtractionTests(unittest.TestCase):
    def test_removes_only_standalone_chapter_lines_and_keeps_boundaries(self) -> None:
        text = (
            "Chapter 1\n"
            "The first chapter ended.\n"
            "\n"
            "cHaPtEr 2\n"
            "The second chapter began."
        )

        result = extract_chapters(text, pause_seconds=1.25)

        self.assertEqual(
            result.narration_text,
            "The first chapter ended.\n\nThe second chapter began.",
        )
        self.assertEqual(result.subtitle_text, result.narration_text)
        self.assertEqual([item.number for item in result.boundaries], [1, 2])
        self.assertEqual([item.source_line for item in result.boundaries], [1, 4])
        self.assertEqual(result.boundaries[0].pause_before_seconds, 0.0)
        self.assertEqual(result.boundaries[1].pause_before_seconds, 1.25)
        self.assertEqual(result.boundaries[0].character_offset, 0)
        self.assertEqual(
            result.boundaries[1].character_offset,
            result.narration_text.index("The second"),
        )
        self.assertEqual([item.sentence_index for item in result.boundaries], [0, 1])

    def test_does_not_remove_chapter_words_inside_content(self) -> None:
        text = "Chapter 4: The Return\nThis chapter 4 reference stays."
        result = extract_chapters(text)
        self.assertEqual(result.narration_text, text)
        self.assertEqual(result.boundaries, ())


class SentenceSegmentationTests(unittest.TestCase):
    def test_protects_titles_acronyms_and_quoted_dialogue_tags(self) -> None:
        text = (
            'Dr. Smith entered the U.S. Embassy. "Are you ready?" she asked. '
            'He said, "Yes, I am." Then he left.'
        )

        self.assertEqual(
            split_english_sentences(text),
            (
                "Dr. Smith entered the U.S. Embassy.",
                '\"Are you ready?\" she asked.',
                'He said, \"Yes, I am.\"',
                "Then he left.",
            ),
        )

    def test_splits_acronym_when_next_word_is_sentence_starter(self) -> None:
        self.assertEqual(
            split_english_sentences("She moved to the U.S. It was difficult."),
            ("She moved to the U.S.", "It was difficult."),
        )

    def test_handles_decimals_and_text_without_terminal_punctuation(self) -> None:
        self.assertEqual(
            split_english_sentences("It rose 1.5 percent. Then it stopped"),
            ("It rose 1.5 percent.", "Then it stopped"),
        )

    def test_blank_line_is_a_boundary_without_terminal_punctuation(self) -> None:
        self.assertEqual(
            split_english_sentences(
                'The woman said, "Your husband is so wild in bed"\n\n'
                "I was completely confused and asked who she was."
            ),
            (
                'The woman said, "Your husband is so wild in bed"',
                "I was completely confused and asked who she was.",
            ),
        )

    def test_splits_japanese_punctuation_without_following_spaces(self) -> None:
        self.assertEqual(
            split_english_sentences("秘密を知った。本当なの？彼女は走った！「待って。」"),
            ("秘密を知った。", "本当なの？", "彼女は走った！", "「待って。」"),
        )


class StatisticsTests(unittest.TestCase):
    def test_counts_contractions_hyphenated_words_acronyms_and_numbers(self) -> None:
        text = "I don't know the king-sized U.S. room at 39 weeks."
        self.assertEqual(count_words(text), 10)

    def test_estimates_duration_by_wpm(self) -> None:
        self.assertEqual(estimate_duration_seconds(120, wpm=120), 60.0)
        stats = calculate_text_statistics(
            " ".join("word" for _ in range(120)),
            wpm=120,
            chapter_pause_seconds=2.5,
        )
        self.assertEqual(stats.word_count, 120)
        self.assertEqual(stats.estimated_speech_seconds, 60.0)
        self.assertEqual(stats.estimated_total_seconds, 62.5)
        self.assertAlmostEqual(stats.estimated_total_minutes, 62.5 / 60.0)

    def test_counts_accented_latin_words_once(self) -> None:
        self.assertEqual(count_words("Él llegó al corazón; François répondit très vite."), 8)

    def test_counts_alphanumeric_story_tokens_as_words(self) -> None:
        self.assertEqual(count_words("word0 word119 B73165"), 3)

    def test_estimates_japanese_as_spoken_planning_units(self) -> None:
        prose = "秘密を知った彼女はすぐに走り出した。" * 20
        units = count_words(prose)
        self.assertGreater(units, 150)
        self.assertGreater(estimate_duration_seconds(units, wpm=210), 40)

    def test_rejects_invalid_duration_inputs(self) -> None:
        for wpm in (0, -1, math.inf, math.nan):
            with self.subTest(wpm=wpm), self.assertRaises(ValueError):
                estimate_duration_seconds(10, wpm=wpm)
        with self.assertRaises(ValueError):
            estimate_duration_seconds(-1)


class ManuscriptAnalysisTests(unittest.TestCase):
    def test_builds_structured_analysis_from_sample_like_manuscript(self) -> None:
        raw = (
            "Chapter 1\r\n"
            "My husband\u9225\u6a9a secret was out.\r\n"
            "Chapter 2\r\n"
            "\u201cRun!\u201d she said."
        )

        analysis = analyze_manuscript(
            raw,
            "123456_Betrayed While Pregnant.txt",
            wpm=160,
            chapter_pause_seconds=1.25,
        )

        self.assertEqual(analysis.code, "123456")
        self.assertEqual(analysis.title, "Betrayed While Pregnant")
        self.assertNotIn("Chapter", analysis.narration_text)
        self.assertEqual(analysis.subtitle_text, analysis.narration_text)
        self.assertIn("husband's", analysis.narration_text)
        self.assertEqual(
            analysis.sentences,
            ("My husband's secret was out.", '\"Run!\" she said.'),
        )
        self.assertEqual(analysis.word_count, 8)
        self.assertAlmostEqual(analysis.statistics.estimated_speech_seconds, 3.0)
        self.assertAlmostEqual(analysis.statistics.chapter_pause_seconds, 1.25)
        self.assertAlmostEqual(analysis.estimated_duration_seconds, 4.25)
        self.assertEqual([item.sentence_index for item in analysis.chapters], [0, 1])

    def test_rejects_empty_manuscript(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty"):
            analyze_manuscript(" \n ", "123_Story.txt")


if __name__ == "__main__":
    unittest.main()
