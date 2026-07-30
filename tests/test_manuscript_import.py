from __future__ import annotations

import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from storyforge.services.manuscript_import import (
    plan_video_episodes,
    prepare_manuscript,
    read_manuscript_file,
    split_source_chapters,
)


class ManuscriptFileImportTests(unittest.TestCase):
    def test_reads_txt_and_rejects_legacy_doc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            txt = root / "A9_The Promise.txt"
            txt.write_text("Last night, the phone rang.", encoding="utf-8")
            self.assertEqual(read_manuscript_file(txt), "Last night, the phone rang.")

            legacy = root / "story.doc"
            legacy.write_bytes(b"not a real doc")
            with self.assertRaisesRegex(ValueError, "docx"):
                read_manuscript_file(legacy)

    def test_reads_unicode_txt_boms_before_legacy_encodings(self) -> None:
        samples = (
            ("utf8-bom.txt", "Chapter 1\nCafé déjà vu.", "utf-8-sig"),
            ("utf16-le.txt", "第一章\n电话突然响了。", "utf-16"),
            ("utf16-be.txt", "第一章\n電話が突然鳴った。", "utf-16-be"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for filename, expected, encoding in samples:
                with self.subTest(encoding=encoding):
                    path = root / filename
                    raw = expected.encode(encoding)
                    if encoding == "utf-16-be":
                        raw = b"\xfe\xff" + raw
                    path.write_bytes(raw)
                    self.assertEqual(read_manuscript_file(path), expected)

    def test_reads_common_legacy_novel_encodings_without_cp1252_mojibake(self) -> None:
        samples = (
            (
                "chinese.txt",
                "第一章\n昨晚十点，电话突然响了。她知道我丈夫的名字。",
                "gb18030",
            ),
            (
                "japanese.txt",
                "第一章\n昨夜十時、電話が突然鳴った。彼女は夫の名前を知っていた。",
                "shift_jis",
            ),
            (
                "western.txt",
                "Chapter 1\nCafé déjà vu — voilà. “Don’t go,” she said.",
                "cp1252",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for filename, expected, encoding in samples:
                with self.subTest(encoding=encoding):
                    path = root / filename
                    path.write_bytes(expected.encode(encoding))
                    self.assertEqual(read_manuscript_file(path), expected)

    def test_rejects_binary_and_unreadable_txt_payloads(self) -> None:
        payloads = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
            bytes(range(256)),
            b"\xc0\xc1\xc2\xc3\xc4\xc5\xc6\xc7" * 8,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, payload in enumerate(payloads):
                with self.subTest(index=index):
                    path = root / f"broken-{index}.txt"
                    path.write_bytes(payload)
                    with self.assertRaisesRegex(ValueError, "无法读取"):
                        read_manuscript_file(path)

    def test_reads_docx_without_an_external_dependency(self) -> None:
        document_xml = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body>
            <w:p><w:r><w:t>Chapter 1</w:t></w:r></w:p>
            <w:p><w:r><w:t>The call came at ten.</w:t></w:r></w:p>
            <w:p><w:r><w:t>She knew my husband's name.</w:t></w:r></w:p>
          </w:body>
        </w:document>'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "story.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", document_xml)
            text = read_manuscript_file(path)
        self.assertIn("Chapter 1", text)
        self.assertIn("husband's name", text)


class ChapterAndEpisodePlanningTests(unittest.TestCase):
    def test_japanese_authored_episodes_have_realistic_duration(self) -> None:
        text = "\n\n".join(
            f"第{index}話\n" + ("彼女は秘密を知り、真実を確かめるために走り続けた。" * 35)
            for index in range(1, 5)
        )
        prepared = prepare_manuscript(text, title="日本語ストーリー", wpm=210)

        self.assertEqual(len(prepared.episodes), 4)
        self.assertTrue(all(item.word_count > 350 for item in prepared.episodes))
        self.assertTrue(all(item.estimated_seconds > 100 for item in prepared.episodes))
        self.assertEqual("".join(item.text for item in prepared.episodes).replace("\n", ""), "".join(item.text for item in prepared.chapters).replace("\n", ""))

    def test_chapters_are_preserved_as_real_content_units(self) -> None:
        chapters = split_source_chapters(
            "Chapter 1\nThe phone rang.\n\nChapter 2 - The Lie\nHe denied everything."
        )
        self.assertEqual([item.ordinal for item in chapters], [1, 2])
        self.assertEqual(chapters[1].heading, "Chapter 2 - The Lie")
        self.assertTrue(all(item.is_explicit_boundary for item in chapters))
        self.assertNotIn("Chapter 2", chapters[1].text)

    def test_localized_source_episode_headings_are_detected(self) -> None:
        cases = (
            "Episode 1 - The Call",
            "Capítulo 2: La verdad",
            "Chapitre 3 — Le secret",
            "Kapitel 4 - Heimkehr",
            "Bab 5: Pengkhianatan",
            "第6集：真相",
            "第七章 重逢",
            "第8話 - 帰還",
            "제9화: 귀환",
        )
        text = "\n\n".join(
            f"{heading}\nSource body {index}."
            for index, heading in enumerate(cases, start=1)
        )

        chapters = split_source_chapters(text)

        expected_headings = list(cases)
        expected_headings[2] = "Chapitre 3—Le secret"
        self.assertEqual([item.heading for item in chapters], expected_headings)
        self.assertTrue(all(item.is_explicit_boundary for item in chapters))

    def test_hook_before_first_localized_heading_joins_the_first_episode(self) -> None:
        cases = (
            (
                "He promised he would never betray me.\n\n"
                "Bab 1: Pengkhianatan\nThe message proved that he had lied.\n\n"
                "Bab 2: Keputusan\nI packed my bags before dawn.",
                "Bab 1: Pengkhianatan",
                "He promised he would never betray me.",
                "The message proved that he had lied.",
            ),
            (
                "彼女から届いた写真には、夫が写っていた。\n\n"
                "第1話：秘密\n私はその夜、真実を確かめに行った。\n\n"
                "第2話：選択\n玄関の扉は開いていた。",
                "第1話：秘密",
                "彼女から届いた写真には、夫が写っていた。",
                "私はその夜、真実を確かめに行った。",
            ),
        )

        for text, heading, hook, opening_body in cases:
            with self.subTest(heading=heading):
                chapters = split_source_chapters(text)
                episodes = plan_video_episodes(chapters)

                self.assertEqual(len(chapters), 2)
                self.assertEqual(chapters[0].heading, heading)
                self.assertTrue(chapters[0].is_explicit_boundary)
                self.assertLess(chapters[0].text.index(hook), chapters[0].text.index(opening_body))
                self.assertEqual(len(episodes), 2)
                self.assertEqual(episodes[0].title, heading)
                self.assertTrue(all(not item.title.startswith("E") for item in episodes))

    def test_short_chapters_merge_and_long_content_splits_without_loss(self) -> None:
        text = "\n\n".join(
            f"Chapter {number}\n" + " ".join(
                f"Sentence {index} ended with a secret."
                for index in range(45)
            )
            for number in range(1, 4)
        )
        chapters = split_source_chapters(text)
        episodes = plan_video_episodes(
            chapters,
            wpm=120,
            target_minutes=1.0,
            maximum_minutes=1.4,
        )
        self.assertGreater(len(episodes), 1)
        self.assertEqual(
            sum(item.word_count for item in episodes),
            sum(item.word_count for item in chapters),
        )
        self.assertEqual(
            [item.ordinal for item in episodes],
            list(range(1, len(episodes) + 1)),
        )
        source = re.sub(
            r"\s+",
            " ",
            "\n\n".join(chapter.text for chapter in chapters),
        ).strip()
        planned = re.sub(
            r"\s+",
            " ",
            "\n\n".join(episode.text for episode in episodes),
        ).strip()
        self.assertEqual(planned, source)
        cursor = 0
        for episode in episodes:
            self.assertEqual(episode.source_start_word, cursor)
            self.assertEqual(
                episode.source_end_word - episode.source_start_word,
                episode.word_count,
            )
            cursor = episode.source_end_word
        self.assertEqual(cursor, sum(chapter.word_count for chapter in chapters))

    def test_genuinely_short_explicit_chapters_never_merge(self) -> None:
        chapters = split_source_chapters(
            "Chapter 1\nAlpha walked into the station.\n\n"
            "Chapter 2\nBeta followed without saying anything.\n\n"
            "Chapter 3\nGamma locked the door behind them."
        )
        episodes = plan_video_episodes(
            chapters,
            wpm=40,
            target_minutes=1.0,
            maximum_minutes=1.25,
        )

        self.assertEqual(len(episodes), 3)
        self.assertEqual(
            [item.source_chapter_ordinals for item in episodes],
            [(1,), (2,), (3,)],
        )
        self.assertEqual(
            [item.title for item in episodes],
            ["Chapter 1", "Chapter 2", "Chapter 3"],
        )
        self.assertTrue(all(item.explicit_source_boundary for item in episodes))

    def test_unheaded_manuscript_keeps_automatic_episode_planning(self) -> None:
        text = " ".join(
            f"Automatic sentence {index} moved the story onward."
            for index in range(30)
        )
        chapters = split_source_chapters(text)
        episodes = plan_video_episodes(
            chapters,
            wpm=45,
            target_minutes=1.0,
            maximum_minutes=1.3,
        )

        self.assertFalse(chapters[0].is_explicit_boundary)
        self.assertGreater(len(episodes), 1)
        self.assertEqual(
            [item.title for item in episodes],
            [f"E{index:03d}" for index in range(1, len(episodes) + 1)],
        )
        self.assertTrue(all(not item.explicit_source_boundary for item in episodes))

    def test_long_chapter_prefers_narrative_boundaries_near_target(self) -> None:
        cases = (
            ("Who had hidden the final letter?", "suspense question"),
            ("I discovered that he was alive.", "revelation boundary"),
            ("However the sealed door swung open.", "turning-point boundary"),
            ("A phone rang inside the coffin.", "suspense boundary"),
        )

        for cue, expected_reason in cases:
            with self.subTest(cue=cue):
                before = [
                    f"Routine detail {index} moved onward."
                    for index in range(8)
                ]
                after = [
                    f"Later detail {index} moved onward."
                    for index in range(6)
                ]
                chapters = split_source_chapters(
                    "Chapter 1\n" + " ".join([*before, cue, *after])
                )
                episodes = plan_video_episodes(
                    chapters,
                    wpm=50,
                    target_minutes=1.0,
                    maximum_minutes=1.4,
                )

                self.assertGreater(len(episodes), 1)
                self.assertTrue(episodes[0].text.endswith(cue))
                self.assertEqual(episodes[0].boundary_reason, expected_reason)
                self.assertEqual(episodes[0].source_heading, "Chapter 1")
                self.assertEqual(
                    [item.source_part_index for item in episodes],
                    list(range(1, len(episodes) + 1)),
                )
                self.assertTrue(
                    all(item.source_part_count == len(episodes) for item in episodes)
                )
                self.assertEqual(
                    episodes[0].title,
                    f"Chapter 1 (1/{len(episodes)})",
                )

    def test_long_explicit_episode_splits_only_inside_its_source_boundary(self) -> None:
        long_body = " ".join(
            f"Long episode sentence {index} carried another hidden clue."
            for index in range(35)
        )
        chapters = split_source_chapters(
            f"Episode 1 - The Search\n{long_body}\n\n"
            "Episode 2 - The Answer\nThe answer arrived in one short message."
        )
        episodes = plan_video_episodes(
            chapters,
            wpm=55,
            target_minutes=1.0,
            maximum_minutes=1.2,
        )

        first_source_parts = [
            item for item in episodes if item.source_chapter_ordinals == (1,)
        ]
        second_source_parts = [
            item for item in episodes if item.source_chapter_ordinals == (2,)
        ]
        self.assertGreater(len(first_source_parts), 1)
        self.assertEqual(len(second_source_parts), 1)
        self.assertTrue(
            all(item.source_heading == "Episode 1 - The Search" for item in first_source_parts)
        )
        self.assertEqual(second_source_parts[0].title, "Episode 2 - The Answer")
        self.assertTrue(
            all(len(item.source_chapter_ordinals) == 1 for item in episodes)
        )

    def test_prepare_returns_stable_hash_and_internal_episode_names(self) -> None:
        first = prepare_manuscript(
            "Chapter 1\nThe phone rang.\n\nChapter 2\nI answered it.",
            title="The Call",
        )
        second = prepare_manuscript(
            "Chapter 1\r\nThe phone rang.\r\n\r\nChapter 2\r\nI answered it.",
            title="Another label",
        )
        self.assertEqual(first.content_sha256, second.content_sha256)
        self.assertEqual(first.episodes[0].title, "Chapter 1")
        self.assertEqual(first.episodes[1].title, "Chapter 2")
        self.assertEqual(first.title, "The Call")


if __name__ == "__main__":
    unittest.main()
