from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from storyforge.services.subtitles import (
    AssStyleConfig,
    NarrationSentence,
    SubtitleCue,
    build_semantic_cues,
    build_sentence_cues,
    colour_to_ass,
    generate_ass,
    paginate_sentence,
    parse_narration_text,
    seconds_to_ass_time,
    split_semantic_phrases,
    wrap_sentence,
    write_ass,
)


class TimelineTests(unittest.TestCase):
    def test_chapter_headings_are_hidden_and_shift_later_timecodes(self) -> None:
        items = [
            NarrationSentence("Chapter 1", is_chapter=True),
            NarrationSentence("The first sentence.", duration=2.0),
            NarrationSentence("Chapter 2"),
            NarrationSentence("The second sentence.", duration=3.0),
        ]

        timeline = build_sentence_cues(items, chapter_pause=0.75)

        self.assertEqual(timeline.chapter_pause_count, 2)
        self.assertEqual([cue.text for cue in timeline.cues], ["The first sentence.", "The second sentence."])
        self.assertAlmostEqual(timeline.cues[0].start, 0.75)
        self.assertAlmostEqual(timeline.cues[0].end, 2.75)
        self.assertAlmostEqual(timeline.cues[1].start, 3.5)
        self.assertAlmostEqual(timeline.cues[1].end, 6.5)
        self.assertAlmostEqual(timeline.total_duration, 6.5)

    def test_text_parser_keeps_chapters_but_splits_complete_sentences(self) -> None:
        units = parse_narration_text(
            "Chapter 1\nLast night was strange. I couldn't sleep!\n\n第2章\nThen she called?"
        )

        self.assertEqual(len(units), 5)
        self.assertTrue(units[0].is_chapter)
        self.assertEqual(units[1].text, "Last night was strange.")
        self.assertEqual(units[2].text, "I couldn't sleep!")
        self.assertTrue(units[3].is_chapter)
        self.assertEqual(units[4].text, "Then she called?")

    def test_explicit_gap_after_sentence_is_preserved(self) -> None:
        timeline = build_sentence_cues(
            [
                NarrationSentence("One.", duration=1.0, gap_after=0.4),
                NarrationSentence("Two.", duration=1.0),
            ]
        )
        self.assertAlmostEqual(timeline.cues[1].start, 1.4)


class SemanticCaptionTests(unittest.TestCase):
    def test_short_phrases_keep_english_meaning_groups_together(self) -> None:
        text = (
            "When Sarah Collins finally looked up, she could not believe what "
            "Daniel Hart had found out behind the locked door."
        )

        phrases = split_semantic_phrases(text)

        self.assertEqual(" ".join(phrases), text)
        self.assertTrue(all(3 <= len(phrase.split()) <= 8 for phrase in phrases))
        for protected in ("Sarah Collins", "could not", "Daniel Hart", "found out"):
            self.assertTrue(any(protected in phrase for phrase in phrases), protected)

    def test_semantic_cues_fill_real_sentence_time_without_gaps(self) -> None:
        source = SubtitleCue(
            1.25,
            9.25,
            "She opened the letter, but she could not accept what Daniel Hart had written.",
        )

        cues = build_semantic_cues([source])

        self.assertGreater(len(cues), 1)
        self.assertAlmostEqual(cues[0].start, source.start)
        self.assertAlmostEqual(cues[-1].end, source.end)
        self.assertTrue(
            all(
                abs(current.end - following.start) < 1e-9
                for current, following in zip(cues, cues[1:])
            )
        )
        self.assertEqual(" ".join(cue.text for cue in cues), source.text)

    def test_semantic_ass_mode_is_optional_and_caps_each_event_at_two_lines(self) -> None:
        cue = SubtitleCue(
            0.0,
            6.0,
            "She never knew Daniel Hart had found out everything.",
        )
        sentence_content = generate_ass(
            [cue],
            platform="GoodNovel",
            code="B73165",
            video_duration=6.0,
            config=AssStyleConfig(max_chars_per_line=60),
        )
        semantic_content = generate_ass(
            [cue],
            platform="GoodNovel",
            code="B73165",
            video_duration=6.0,
            config=AssStyleConfig(
                max_chars_per_line=14,
                max_subtitle_lines=4,
                semantic_short_phrases=True,
            ),
        )

        sentence_events = [
            line for line in sentence_content.splitlines() if line.startswith("Dialogue: 0,")
        ]
        semantic_events = [
            line for line in semantic_content.splitlines() if line.startswith("Dialogue: 0,")
        ]
        self.assertEqual(len(sentence_events), 1)
        self.assertGreater(len(semantic_events), 1)
        self.assertTrue(
            all(len(line.split(",", 9)[9].split(r"\N")) <= 2 for line in semantic_events)
        )


class AssGenerationTests(unittest.TestCase):
    def test_classic_preview_has_a_timed_opening_hook(self) -> None:
        content = generate_ass(
            [SubtitleCue(4.0, 7.0, "The first spoken sentence.")],
            platform="GoodNovel",
            code="B39760",
            video_duration=15.0,
            end_card_title="A Dangerous Secret",
            end_card_action="Download GoodNovel and search code B39760.",
            end_card_duration=5.0,
            video_template="classic",
            intro_headline="The call exposed her husband's double life.",
            intro_card_duration=4.0,
        )

        hook_event = next(
            line
            for line in content.splitlines()
            if ",IntroHeadline," in line and line.startswith("Dialogue: 4,")
        )
        self.assertIn("0:00:00.08,0:00:04.00", hook_event)
        self.assertIn(r"\an8\move(540,", hook_event)
        self.assertIn("husband's", hook_event)

        full_video_content = generate_ass(
            [SubtitleCue(0.0, 7.0, "The full narration starts immediately.")],
            platform="GoodNovel",
            code="B39760",
            video_duration=15.0,
            video_template="classic",
            intro_headline="This must not cover the full video's subtitles.",
            intro_card_duration=0.0,
        )
        self.assertFalse(
            any(
                ",IntroHeadline," in line
                for line in full_video_content.splitlines()
                if line.startswith("Dialogue:")
            )
        )

    def test_word_sync_uses_increasing_real_word_windows_and_three_states(self) -> None:
        content = generate_ass(
            [SubtitleCue(0.0, 3.0, "One two three.")],
            platform="GoodNovel",
            code="B73165",
            video_duration=3.0,
            config=AssStyleConfig(
                word_sync_enabled=True,
                word_unread_color="#AABBCC",
                word_active_color="#FFD400",
                word_read_color="#11EE88",
                word_pop_scale=124,
                word_pop_duration_ms=180,
                word_pop_intensity=0.5,
            ),
        )

        word_events = [
            line.split(",", 9)
            for line in content.splitlines()
            if line.startswith("Dialogue: 0,")
        ]
        self.assertEqual(len(word_events), 3)
        self.assertEqual(word_events[0][1], "0:00:00.00")
        self.assertEqual(word_events[-1][2], "0:00:03.00")
        self.assertTrue(
            all(current[2] == following[1] for current, following in zip(word_events, word_events[1:]))
        )
        # Active colour is #FFD400 -> ASS BBGGRR 00D4FF.  The 12% effective
        # pop is derived from (124-100)*0.5.  Only vertical scale changes so
        # libass cannot reflow the line and move its centred anchor per word.
        self.assertTrue(all(r"\1c&H0000D4FF" in event[9] for event in word_events))
        self.assertTrue(all(r"\fscx100\fscy112" in event[9] for event in word_events))
        self.assertFalse(any(r"\fscx112" in event[9] for event in word_events))
        self.assertTrue(all(r"\t(0," in event[9] for event in word_events))
        self.assertTrue(all(r"\fscy100)}}" not in event[9] for event in word_events))
        self.assertIn(r"\1c&H00CCBBAA", word_events[0][9])
        self.assertIn(r"\1c&H0088EE11", word_events[-1][9])

    def test_large_word_sync_caption_is_centered_and_clipped_to_hard_safe_rails(self) -> None:
        content = generate_ass(
            [
                SubtitleCue(
                    0.0,
                    5.0,
                    "She opened the message and discovered the secret he kept for years.",
                )
            ],
            platform="GoodNovel",
            code="B39760",
            video_duration=5.0,
            config=AssStyleConfig(
                subtitle_font_size=78,
                subtitle_margin_left=180,
                subtitle_margin_right=180,
                subtitle_margin_bottom=310,
                subtitle_position_x_percent=50,
                subtitle_alignment="center",
                max_chars_per_line=28,
                max_subtitle_lines=3,
                subtitle_background_opacity=0.12,
                word_sync_enabled=True,
                word_pop_scale=124,
            ),
        )

        subtitle_style = next(
            line for line in content.splitlines() if line.startswith("Style: Subtitle")
        )
        self.assertIn(",78,", subtitle_style)
        self.assertIn(",2,188,188,360,1", subtitle_style)
        caption_events = [
            line.split(",", 9)[9]
            for line in content.splitlines()
            if line.startswith("Dialogue: 0,")
        ]
        self.assertGreater(len(caption_events), 1)
        expected_anchor = r"{\an2\pos(540,1560)\q2\clip(188,0,892,1920)}"
        self.assertTrue(all(event.startswith(expected_anchor) for event in caption_events))
        self.assertTrue(all(event.count(r"\N") <= 2 for event in caption_events))
        self.assertTrue(all(r"\fscx100" in event for event in caption_events))
        self.assertFalse(any(r"\fscx124" in event for event in caption_events))

    def test_custom_component_positions_are_clamped_inside_safe_rails(self) -> None:
        content = generate_ass(
            [SubtitleCue(0.0, 10.0, "A safe custom subtitle.")],
            platform="GoodNovel",
            code="B73165",
            video_duration=16.0,
            end_card_title="Continue reading",
            end_card_action="Open the app and search the code.",
            end_card_duration=6.0,
            video_template="platform_story_card",
            intro_card_text="A short factual synopsis.",
            config=AssStyleConfig(
                subtitle_alignment="left",
                subtitle_position_x_percent=10,
                intro_position_x_percent=20,
                intro_width_percent=82,
                intro_position_y_percent=58,
                outro_position_x_percent=80,
                outro_width_percent=82,
                card_position_x_percent=90,
                card_width_percent=82,
            ),
        )

        # Even extreme percentages cannot move any panel outside the symmetric
        # 188px interaction-safe rail on the 1080px delivery canvas.
        self.assertIn(r"\pos(188,", content)
        self.assertNotIn(r"\pos(0,", content)
        self.assertIn(r"\an1\pos(188,", content)

    def test_half_resolution_story_card_scales_type_and_wraps_inside_panel(self) -> None:
        document = generate_ass(
            [SubtitleCue(0.0, 20.0, "The caller knew her husband's name.")],
            platform="GoodNovel",
            code="B73165",
            video_duration=30.0,
            end_card_title="The Call at Ten",
            end_card_action="Download GoodNovel and search code B73165 to continue.",
            end_card_duration=6.0,
            video_template="platform_story_card",
            intro_headline=(
                "Last night at ten o'clock, when I was getting ready to sleep"
            ),
            intro_card_text=(
                "Last night at ten o'clock, Jen received a call from an unknown "
                "woman who knew her name. The stranger claimed Jen's husband was "
                "wild in bed, then sent a video exposing what he was really doing."
            ),
            config=AssStyleConfig(
                play_res_x=540,
                play_res_y=960,
                subtitle_font_size=31,
                card_font_size=24,
                subtitle_margin_left=80,
                subtitle_margin_right=80,
                subtitle_margin_bottom=130,
            ),
        )

        subtitle_style = next(
            line for line in document.splitlines() if line.startswith("Style: Subtitle")
        )
        self.assertIn(",94,94,180,1", subtitle_style)
        self.assertIn("Style: IntroHeadline,Arial,35,", document)
        self.assertIn("Style: IntroSummary,Arial,18,", document)
        self.assertIn("Style: EndTitle,Arial,33,", document)
        self.assertIn(r"\move(94,269,94,260", document)
        self.assertIn(r"\pos(270,75)", document)
        self.assertIn(r"\move(270,127,270,118", document)
        self.assertIn(r"\move(270,371,270,362", document)
        self.assertIn(r"\pos(270,490)", document)
        self.assertIn(r"\pos(270,581)", document)
        summary_event = next(
            line
            for line in document.splitlines()
            if line.startswith("Dialogue: 3,") and ",IntroSummary," in line
        )
        summary_lines = summary_event.split(",", 9)[9].split(r"\N")
        summary_lines[0] = summary_lines[0].split("}", 1)[-1]
        self.assertLessEqual(len(summary_lines), 5)
        self.assertTrue(all(len(line) <= 40 for line in summary_lines))

    def test_platform_story_card_has_intro_panel_persistent_code_and_end_card_phases(self) -> None:
        document = generate_ass(
            [SubtitleCue(0.0, 20.0, "The locked door opened just before midnight.")],
            platform="GoodNovel",
            code="B73165",
            video_duration=20.0,
            end_card_title="The Midnight Call",
            end_card_action="Open GoodNovel to continue reading.",
            end_card_duration=6.0,
            video_template="platform_story_card",
            intro_headline="A secret call changes everything",
            intro_card_text=(
                "A stranger knows the truth about Mara's husband and sends her "
                "to an address that should not exist."
            ),
            intro_card_duration=5.5,
            platform_brand_color="#E43D59",
        )

        for style in (
            "TemplateShadow",
            "TemplatePanel",
            "TemplateAccent",
            "IntroHeadline",
            "IntroBadge",
            "IntroPlatform",
            "IntroSummary",
            "IntroFooter",
        ):
            self.assertIn(f"Style: {style}", document)
        self.assertIn(r"\p1\1c&H00FFFFFF&", document)
        self.assertIn(r"A secret call\Nchanges everything", document)
        self.assertIn("A stranger knows the truth", document)
        self.assertIn("STORY BRIEF", document)
        self.assertNotIn("STORY PREVIEW", document)
        self.assertNotRegex(document, r"(?i)part\s+\d+(?:\s+of\s+\d+)?")
        self.assertIn(r"\move(188,538,188,520", document)
        self.assertIn(r"\pos(540,150)", document)
        self.assertIn(r"\move(540,254,540,236", document)
        self.assertIn(r"\pos(540,646)", document)
        self.assertIn(r"\move(540,742,540,724", document)
        self.assertIn(r"\move(504,560,504,542", document)
        self.assertIn('GoodNovel  ·  Search “B73165”', document)
        self.assertIn(r"\1c&H00593DE4", document)
        self.assertNotIn("SAVE", document)
        self.assertNotIn("SHARE", document)
        self.assertNotIn(r"\pos(188,552)", document)

        events = [
            line.split(",", 9)
            for line in document.splitlines()
            if line.startswith("Dialogue:")
        ]
        by_style: dict[str, list[list[str]]] = {}
        for event in events:
            by_style.setdefault(event[3], []).append(event)
        self.assertEqual(len(by_style["IntroFooter"]), 1)

        intro_styles = {
            "TemplateShadow",
            "TemplatePanel",
            "TemplateAccent",
            "IntroHeadline",
            "IntroBadge",
            "IntroPlatform",
            "IntroSummary",
            "IntroFooter",
        }
        intro_events = [
            event
            for event in events
            if event[3] in intro_styles and event[1] < "0:00:05.50"
        ]
        self.assertTrue(intro_events)
        self.assertTrue(all(event[2] == "0:00:05.50" for event in intro_events))

        search_event = by_style["SearchCard"][0]
        self.assertEqual(search_event[1:3], ["0:00:05.50", "0:00:14.00"])
        self.assertIn("Search GoodNovel: B73165", search_event[9])

        end_events = [
            event
            for event in events
            if event[3] in {"EndTitle", "EndAction", "EndCode"}
        ]
        self.assertEqual(len(end_events), 3)
        self.assertTrue(all(event[1] >= "0:00:14.00" for event in end_events))
        self.assertTrue(all(event[2] == "0:00:20.00" for event in end_events))

        # The three card phases are adjacent, never stacked on top of one
        # another: intro -> persistent search command -> closing card.
        self.assertEqual(intro_events[0][2], search_event[1])
        closing_panel = next(
            event
            for event in by_style["TemplatePanel"]
            if event[1] == "0:00:14.00"
        )
        self.assertEqual(search_event[2], closing_panel[1])

    def test_story_card_wraps_cjk_summary_without_spaces_into_five_lines(self) -> None:
        document = generate_ass(
            [SubtitleCue(0.0, 12.0, "她终于发现了藏在信封里的秘密。")],
            platform="GoodNovel",
            code="B73165",
            video_duration=18.0,
            end_card_title="午夜来电",
            end_card_action="打开GoodNovel搜索口令B73165继续阅读。",
            end_card_duration=6.0,
            video_template="platform_story_card",
            intro_headline="一通电话毁掉了她平静的生活",
            intro_card_text=(
                "结婚纪念日前夜她接到陌生女人的电话，对方不仅准确说出丈夫的名字，"
                "还发来一段足以摧毁婚姻的视频。她循着线索来到酒店，却发现真正的背叛"
                "远比想象中复杂，而那个女人似乎一直在等待她出现。"
            ),
        )

        summary_event = next(
            line
            for line in document.splitlines()
            if line.startswith("Dialogue: 3,") and ",IntroSummary," in line
        )
        summary_lines = summary_event.split(",", 9)[9].split(r"\N")
        summary_lines[0] = summary_lines[0].split("}", 1)[-1]
        self.assertEqual(len(summary_lines), 5)
        self.assertTrue(summary_lines[0].startswith("结婚纪念日前夜"))
        self.assertTrue(all(1 <= len(line) <= 23 for line in summary_lines))

    def test_final_label_is_optional_and_numbered_part_labels_are_rejected(self) -> None:
        ordinary = generate_ass(
            [SubtitleCue(0.0, 7.0, "The truth was waiting behind the door.")],
            platform="GoodNovel",
            code="B73165",
            video_duration=14.0,
            end_card_duration=6.0,
            end_card_title="The Locked Door",
            video_template="platform_story_card",
            intro_card_text="A woman follows one final clue to a room that should be empty.",
        )
        final = generate_ass(
            [SubtitleCue(0.0, 7.0, "The truth was waiting behind the door.")],
            platform="GoodNovel",
            code="B73165",
            video_duration=14.0,
            end_card_duration=6.0,
            end_card_title="The Locked Door",
            video_template="platform_story_card",
            intro_card_text="A woman follows one final clue to a room that should be empty.",
            final_label="FINAL PART",
        )

        self.assertIn("STORY BRIEF", ordinary)
        self.assertNotIn("FINAL PART", ordinary)
        self.assertIn("FINAL PART", final)
        with self.assertRaisesRegex(ValueError, "numbered Part"):
            generate_ass(
                [SubtitleCue(0.0, 7.0, "The truth was waiting behind the door.")],
                platform="GoodNovel",
                code="B73165",
                video_duration=14.0,
                end_card_duration=6.0,
                end_card_title="The Locked Door",
                video_template="platform_story_card",
                final_label="Part 2 of 5",
            )

    def test_real_platform_logo_reserves_the_ticket_slot_without_badge_bleed(self) -> None:
        document = generate_ass(
            [SubtitleCue(0.0, 7.0, "The truth was waiting behind the door.")],
            platform="GoodNovel",
            code="B73165",
            video_duration=14.0,
            end_card_duration=6.0,
            end_card_title="The Locked Door",
            video_template="platform_story_card",
            intro_card_text="A woman follows one final clue.",
            platform_logo_present=True,
        )

        self.assertNotIn(r"\fad(180,180)}SEARCH", document)
        self.assertIn('GoodNovel  ·  Search “B73165”', document)

    def test_optional_soft_caption_animation_never_animates_search_card(self) -> None:
        content = generate_ass(
            [SubtitleCue(0.0, 2.0, "The secret was inside the letter.")],
            platform="GoodNovel",
            code="B73165",
            video_duration=2.0,
            config=AssStyleConfig(subtitle_animation="soft_pop"),
        )
        subtitle_event = next(
            line for line in content.splitlines() if line.startswith("Dialogue: 0,")
        )
        search_event = next(
            line for line in content.splitlines() if ",SearchCard," in line
        )
        self.assertIn(r"{\fscx94\fscy94\t(0,120,\fscx100\fscy100)\fad(80,0)}", subtitle_event)
        self.assertNotIn(r"\fad", search_event)

    def test_new_caption_animation_ids_survive_safe_normalization(self) -> None:
        for animation in ("rise", "mask_reveal", "typewriter"):
            with self.subTest(animation=animation):
                self.assertEqual(
                    AssStyleConfig(subtitle_animation=animation).safe().subtitle_animation,
                    animation,
                )
        self.assertEqual(
            AssStyleConfig(subtitle_animation="unknown").safe().subtitle_animation,
            "none",
        )

    def test_rise_animation_uses_seek_safe_move_and_keeps_search_static(self) -> None:
        content = generate_ass(
            [SubtitleCue(0.0, 2.0, "She opened the letter.")],
            platform="GoodNovel",
            code="B73165",
            video_duration=2.0,
            config=AssStyleConfig(subtitle_animation="rise"),
        )
        subtitle_event = next(
            line for line in content.splitlines() if line.startswith("Dialogue: 0,")
        )
        search_event = next(
            line for line in content.splitlines() if ",SearchCard," in line
        )

        self.assertIn(r"\move(540,1578,540,1560,0,180)", subtitle_event)
        self.assertIn(r"\clip(188,0,892,1920)", subtitle_event)
        self.assertIn(r"\fad(90,0)", subtitle_event)
        self.assertNotIn(r"\pos(", subtitle_event)
        self.assertNotIn(r"\move(", search_event)

    def test_rise_animation_clamps_motion_to_a_short_cue(self) -> None:
        content = generate_ass(
            [SubtitleCue(0.0, 0.03, "Now.")],
            platform="GoodNovel",
            code="B73165",
            video_duration=0.03,
            config=AssStyleConfig(subtitle_animation="rise"),
        )
        subtitle_event = next(
            line for line in content.splitlines() if line.startswith("Dialogue: 0,")
        )
        self.assertIn(",0,10)", subtitle_event)
        self.assertIn(r"\fad(10,0)", subtitle_event)

    def test_mask_reveal_animates_one_hard_safe_clip_only(self) -> None:
        content = generate_ass(
            [SubtitleCue(0.0, 2.0, "ABC")],
            platform="GoodNovel",
            code="B73165",
            video_duration=2.0,
            config=AssStyleConfig(subtitle_animation="mask_reveal"),
        )
        subtitle_event = next(
            line for line in content.splitlines() if line.startswith("Dialogue: 0,")
        )
        search_event = next(
            line for line in content.splitlines() if ",SearchCard," in line
        )

        self.assertIn(r"\clip(490,0,491,1920)", subtitle_event)
        self.assertIn(r"\t(0,260,\clip(490,0,590,1920))", subtitle_event)
        self.assertNotIn(r"\clip(188,0,892,1920)", subtitle_event)
        self.assertNotIn(r"\t(0,260", search_event)

    def test_typewriter_is_a_contiguous_seek_safe_state_sequence(self) -> None:
        content = generate_ass(
            [SubtitleCue(0.0, 2.0, "ABC")],
            platform="GoodNovel",
            code="B73165",
            video_duration=2.0,
            config=AssStyleConfig(subtitle_animation="typewriter"),
        )
        events = [
            line.split(",", 9)
            for line in content.splitlines()
            if line.startswith("Dialogue: 0,")
        ]

        self.assertEqual(len(events), 3)
        self.assertEqual(
            [(event[1], event[2]) for event in events],
            [
                ("0:00:00.00", "0:00:00.05"),
                ("0:00:00.05", "0:00:00.10"),
                ("0:00:00.10", "0:00:02.00"),
            ],
        )
        self.assertTrue(events[0][9].endswith(r"A{\alpha&HFF&}BC"))
        self.assertTrue(events[1][9].endswith(r"AB{\alpha&HFF&}C"))
        self.assertTrue(events[2][9].endswith("ABC"))
        self.assertNotIn(r"\alpha&HFF&", events[2][9])

    def test_typewriter_caps_long_copy_and_preserves_ass_escaping(self) -> None:
        copy = "Cafe\u0301 {secret} \\ path 日本語 " * 6
        content = generate_ass(
            [SubtitleCue(0.0, 4.0, copy)],
            platform="GoodNovel",
            code="B73165",
            video_duration=4.0,
            config=AssStyleConfig(
                subtitle_animation="typewriter",
                max_chars_per_line=60,
                max_subtitle_lines=4,
            ),
        )
        events = [
            line.split(",", 9)
            for line in content.splitlines()
            if line.startswith("Dialogue: 0,")
        ]

        self.assertLessEqual(len(events), 31 * 2)
        self.assertEqual(events[0][1], "0:00:00.00")
        self.assertEqual(events[-1][2], "0:00:04.00")
        self.assertTrue(
            all(current[2] == following[1] for current, following in zip(events, events[1:]))
        )
        self.assertTrue(all(event[1] != event[2] for event in events))
        self.assertIn(r"\{secret\}", events[-1][9])
        self.assertIn(r"\\ path", events[-1][9])
        self.assertIn("Cafe\u0301", events[-1][9])

    def test_word_sync_takes_precedence_over_caption_entrance_animations(self) -> None:
        for animation in ("rise", "mask_reveal", "typewriter"):
            with self.subTest(animation=animation):
                content = generate_ass(
                    [SubtitleCue(0.0, 2.0, "One final secret")],
                    platform="GoodNovel",
                    code="B73165",
                    video_duration=2.0,
                    config=AssStyleConfig(
                        subtitle_animation=animation,
                        word_sync_enabled=True,
                    ),
                )
                caption = "\n".join(
                    line
                    for line in content.splitlines()
                    if line.startswith("Dialogue: 0,")
                )
                self.assertNotIn(r"\move(", caption)
                self.assertNotIn(r"\t(0,260,\clip", caption)
                self.assertNotIn(r"\alpha&HFF&", caption)

    def test_single_word_mode_shows_only_the_current_spoken_word(self) -> None:
        content = generate_ass(
            [SubtitleCue(0.0, 3.0, "She opened the message")],
            platform="GoodNovel",
            code="B73165",
            video_duration=3.0,
            config=AssStyleConfig(
                word_sync_enabled=True,
                word_display_mode="single",
            ),
        )
        captions = [
            line
            for line in content.splitlines()
            if line.startswith("Dialogue: 0,")
        ]
        self.assertEqual(len(captions), 4)
        visible = [line.rsplit(",", 1)[-1] for line in captions]
        for word, line in zip(("She", "opened", "the", "message"), visible, strict=True):
            self.assertIn(word, line)
            self.assertEqual(
                sum(candidate in line for candidate in ("She", "opened", "the", "message")),
                1,
            )

    def test_intro_animation_ids_are_safe_and_unknown_values_fall_back(self) -> None:
        for animation in (
            "none",
            "fade_rise",
            "soft_scale",
            "side_reveal",
            "layered_story",
            "paper_drop",
        ):
            with self.subTest(animation=animation):
                self.assertEqual(
                    AssStyleConfig(intro_animation=animation).safe().intro_animation,
                    animation,
                )
        self.assertEqual(
            AssStyleConfig(intro_animation="unknown").safe().intro_animation,
            "fade_rise",
        )

    def test_intro_animations_are_layered_seek_safe_and_leave_code_continuous(self) -> None:
        markers = {
            "none": (),
            "fade_rise": (r"\move(", r"\fad(120,180)"),
            "soft_scale": (r"\fscy96\t(0,240,\fscy100)",),
            "side_reveal": (
                r"\clip(188,0,189,1920)",
                r"\t(0,300,\clip(188,0,892,1920))",
            ),
            "layered_story": (
                r"\fscy96\t(0,260,\fscy100)",
                r"\move(",
            ),
            "paper_drop": (r"\move(188,490,188,520,0,260)",),
        }
        intro_style_names = {
            "TemplateShadow",
            "TemplatePanel",
            "TemplateAccent",
            "IntroHeadline",
            "IntroBadge",
            "IntroPlatform",
            "IntroSummary",
            "IntroFooter",
        }
        for animation, expected_markers in markers.items():
            with self.subTest(animation=animation):
                content = generate_ass(
                    [SubtitleCue(5.5, 7.0, "The story continues.")],
                    platform="GoodNovel",
                    code="B73165",
                    video_duration=7.0,
                    video_template="platform_story_card",
                    intro_card_text="A secret letter changes everything.",
                    intro_headline="THE LETTER",
                    intro_card_duration=5.5,
                    config=AssStyleConfig(intro_animation=animation),
                )
                intro_events = [
                    line
                    for line in content.splitlines()
                    if any(f",{style_name}," in line for style_name in intro_style_names)
                    and ",SearchCard," not in line
                ]
                combined_intro = "\n".join(intro_events)
                for marker in expected_markers:
                    self.assertIn(marker, combined_intro)
                if animation == "none":
                    self.assertNotIn(r"\move(", combined_intro)
                    self.assertNotIn(r"\fad(", combined_intro)
                    self.assertNotIn(r"\t(", combined_intro)

                # The platform/code line owns 0.00-5.50 and the persistent
                # SearchCard takes over at exactly 5.50 without a visibility
                # gap or decorative animation.
                platform_event = next(
                    line for line in intro_events if ",IntroPlatform," in line
                )
                search_event = next(
                    line for line in content.splitlines() if ",SearchCard," in line
                )
                self.assertTrue(platform_event.startswith("Dialogue: 3,0:00:00.00,"))
                self.assertIn("B73165", platform_event)
                self.assertNotIn(r"\move(", platform_event)
                self.assertNotIn(r"\fad(", platform_event)
                self.assertNotIn(r"\t(", platform_event)
                self.assertIn(",0:00:05.50,0:00:07.00,SearchCard,", search_event)
                self.assertNotIn(r"\move(", search_event)
                self.assertNotIn(r"\fad(", search_event)
                self.assertNotIn(r"\t(", search_event)

    def test_layered_intro_delays_are_bounded_and_ordered(self) -> None:
        content = generate_ass(
            [SubtitleCue(5.5, 7.0, "The story continues.")],
            platform="GoodNovel",
            code="B73165",
            video_duration=7.0,
            video_template="platform_story_card",
            intro_card_text="A secret letter changes everything.",
            intro_headline="THE LETTER",
            intro_card_duration=5.5,
            config=AssStyleConfig(intro_animation="layered_story"),
        )
        by_style = {
            line.split(",", 9)[3]: line.split(",", 9)
            for line in content.splitlines()
            if line.startswith("Dialogue:")
            and any(
                f",{style_name}," in line
                for style_name in (
                    "TemplatePanel",
                    "IntroHeadline",
                    "IntroSummary",
                    "IntroFooter",
                )
            )
        }
        self.assertEqual(by_style["TemplatePanel"][1], "0:00:00.00")
        self.assertEqual(by_style["IntroHeadline"][1], "0:00:00.08")
        self.assertEqual(by_style["IntroSummary"][1], "0:00:00.32")
        self.assertEqual(by_style["IntroFooter"][1], "0:00:00.44")
        self.assertTrue(all(fields[2] == "0:00:05.50" for fields in by_style.values()))

    def test_classic_intro_headline_uses_selected_animation_without_touching_search(self) -> None:
        content = generate_ass(
            [SubtitleCue(0.0, 3.0, "The story begins.")],
            platform="GoodNovel",
            code="B73165",
            video_duration=3.0,
            video_template="classic",
            intro_headline="THE LETTER",
            intro_card_duration=2.5,
            config=AssStyleConfig(intro_animation="paper_drop"),
        )
        headline_event = next(
            line for line in content.splitlines() if ",IntroHeadline," in line
        )
        search_event = next(
            line for line in content.splitlines() if ",SearchCard," in line
        )
        self.assertIn(r"\move(540,291,540,307,0,260)", headline_event)
        self.assertNotIn(r"\move(", search_event)
        self.assertTrue(search_event.startswith("Dialogue: 7,0:00:00.00,"))

    def test_preview_caption_never_splits_a_word_or_dash_joined_phrase(self) -> None:
        content = generate_ass(
            [
                SubtitleCue(
                    0.0,
                    4.0,
                    "She opened the message—and realized her husband had another life.",
                )
            ],
            platform="GoodNovel",
            code="B73165",
            video_duration=4.0,
            config=AssStyleConfig(
                play_res_x=540,
                play_res_y=960,
                subtitle_font_size=31,
                subtitle_margin_left=180,
                subtitle_margin_right=180,
            ),
        )

        caption_text = "\n".join(
            line.split(",", 9)[9]
            for line in content.splitlines()
            if line.startswith("Dialogue: 0,")
        )
        self.assertIn("message - and", caption_text)
        self.assertNotIn(r"messag\Ne", caption_text)
        self.assertNotIn("—", caption_text)

    def test_end_card_layers_title_action_and_code_above_the_story(self) -> None:
        document = generate_ass(
            [
                SubtitleCue(0.0, 8.0, "The key was already in her hand."),
                SubtitleCue(
                    8.0,
                    12.0,
                    "Download GoodNovel and enter the code to continue.",
                ),
            ],
            platform="GoodNovel",
            code="B73165",
            search_text="Search GoodNovel: B73165",
            video_duration=14.0,
            end_card_title="The Midnight Call",
            end_card_action="Download GoodNovel and enter the code to continue.",
            end_card_duration=6.0,
        )

        self.assertIn("Style: EndTitle", document)
        self.assertIn("Style: EndAction", document)
        self.assertIn("Style: EndCode", document)
        self.assertIn("Dialogue: 3,0:00:08.15,0:00:14.00,EndTitle", document)
        self.assertIn("Dialogue: 4,0:00:08.85,0:00:14.00,EndAction", document)
        self.assertIn("Dialogue: 5,0:00:09.35,0:00:14.00,EndCode", document)
        self.assertIn("The Midnight Call", document)
        self.assertIn("Search GoodNovel: B73165", document)
        subtitle_events = [
            line for line in document.splitlines() if line.startswith("Dialogue: 0,")
        ]
        self.assertTrue(any("The key was already" in line for line in subtitle_events))
        self.assertFalse(any("Download GoodNovel" in line for line in subtitle_events))

    def test_full_duration_search_card_sentence_cues_and_safe_margins(self) -> None:
        cues = [
            SubtitleCue(
                0.8,
                4.0,
                "This sentence is deliberately long enough to wrap cleanly across subtitle lines.",
            ),
            SubtitleCue(4.0, 6.0, "Nothing is highlighted word by word."),
        ]
        unsafe_config = AssStyleConfig(
            subtitle_margin_left=0,
            subtitle_margin_right=0,
            subtitle_margin_bottom=0,
            card_margin_top=0,
            card_margin_right=0,
            max_chars_per_line=24,
        )

        content = generate_ass(
            cues,
            platform="GoodNovel",
            code="123456",
            video_duration=7.5,
            config=unsafe_config,
        )

        self.assertIn("PlayResX: 1080", content)
        self.assertIn("PlayResY: 1920", content)
        self.assertIn("Style: SearchCard", content)
        self.assertIn("Search GoodNovel: 123456", content)
        self.assertIn("Dialogue: 7,0:00:00.00,0:00:07.50,SearchCard", content)
        self.assertNotIn("Chapter 1", content)
        self.assertIn(r"\N", content)
        # Values below the TikTok-safe bounds were automatically corrected.
        subtitle_style = next(line for line in content.splitlines() if line.startswith("Style: Subtitle"))
        search_style = next(line for line in content.splitlines() if line.startswith("Style: SearchCard"))
        self.assertIn(",188,188,360,1", subtitle_style)
        self.assertIn(",150,150,100,1", search_style)
        self.assertIn("WrapStyle: 0", content)

    def test_half_resolution_safe_margins_scale_with_frame(self) -> None:
        content = generate_ass(
            [SubtitleCue(0.0, 2.0, "A short line stays comfortably wide.")],
            platform="GoodNovel",
            code="B73165",
            video_duration=2.0,
            config=AssStyleConfig(
                play_res_x=540,
                play_res_y=960,
                subtitle_margin_left=0,
                subtitle_margin_right=0,
                subtitle_margin_bottom=0,
                card_margin_left=0,
                card_margin_right=0,
                card_margin_top=0,
            ),
        )

        subtitle_style = next(
            line for line in content.splitlines() if line.startswith("Style: Subtitle")
        )
        search_style = next(
            line for line in content.splitlines() if line.startswith("Style: SearchCard")
        )
        self.assertIn(",94,94,180,1", subtitle_style)
        self.assertIn(",60,60,50,1", search_style)

    def test_ass_normalizes_unsupported_dash_without_changing_cjk(self) -> None:
        content = generate_ass(
            [SubtitleCue(0.0, 2.0, "She opened the message\u2014and froze. \u65e5\u672c\u8a9e\u3082\u4fdd\u6301\u3002")],
            platform="GoodNovel",
            code="B73165",
            video_duration=2.0,
        )

        self.assertIn("message - and", content)
        self.assertIn("\u65e5\u672c\u8a9e\u3082\u4fdd\u6301\u3002", content)
        self.assertNotIn("message?and", content)

    def test_special_text_cannot_inject_ass_override_tags(self) -> None:
        content = generate_ass(
            [SubtitleCue(0.0, 2.0, r"A {dangerous} value from C:\\books.")],
            platform="Novel{App}",
            code="42",
            video_duration=2.0,
        )

        self.assertIn(r"Novel\{App\}", content)
        self.assertIn(r"\{dangerous\}", content)
        self.assertNotIn("A {dangerous}", content)

    def test_ass_file_is_utf8_and_timestamp_rounding_carries(self) -> None:
        self.assertEqual(seconds_to_ass_time(59.999), "0:01:00.00")
        self.assertEqual(colour_to_ass("#FF0000"), "&H000000FF")
        self.assertEqual(colour_to_ass("#000000", opacity=0.5), "&H80000000")

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "字幕 with spaces.ass"
            returned = write_ass(
                output,
                [SubtitleCue(0.0, 1.0, "Hello world.")],
                platform="NovelCat",
                code="88",
                video_duration=1.0,
            )
            self.assertEqual(returned, output)
            self.assertIn("Search NovelCat: 88", output.read_text(encoding="utf-8"))

    def test_wrap_keeps_one_sentence_and_never_truncates_words(self) -> None:
        text = "One two three four five six seven eight nine ten eleven twelve."
        wrapped = wrap_sentence(text, width=12, max_lines=2)
        self.assertTrue(all(len(line) <= 12 for line in wrapped.split(r"\N")))
        self.assertEqual(wrapped.replace(r"\N", " "), text)

    def test_long_sentence_is_safely_centered_and_split_into_timed_pages(self) -> None:
        text = (
            'Instead of answering, the woman laughed and said, "Your husband is so wild '
            'in bed." I was completely confused and was about to ask who she was and how '
            "she knew anything about my husband, but the call was abruptly cut off."
        )
        content = generate_ass(
            [SubtitleCue(0.0, 16.0, text)],
            platform="GoodNovel",
            code="B73165",
            video_duration=16.0,
            config=AssStyleConfig(
                subtitle_font_size=52,
                subtitle_margin_left=180,
                subtitle_margin_right=180,
                max_chars_per_line=28,
                max_subtitle_lines=3,
            ),
        )

        style = next(line for line in content.splitlines() if line.startswith("Style: Subtitle"))
        events = [line for line in content.splitlines() if line.startswith("Dialogue: 0,")]
        self.assertIn(",2,188,188,360,1", style)
        self.assertGreater(len(events), 1)
        fields = [line.split(",", 9) for line in events]
        self.assertEqual(fields[0][1], "0:00:00.00")
        self.assertEqual(fields[-1][2], "0:00:16.00")
        self.assertTrue(
            all(current[2] == following[1] for current, following in zip(fields, fields[1:]))
        )
        pages = [field[9].split("}", 1)[-1] for field in fields]
        self.assertTrue(all(len(page.split(r"\N")) <= 3 for page in pages))
        self.assertTrue(
            all(len(line) <= 24 for page in pages for line in page.split(r"\N"))
        )
        self.assertEqual(" ".join(page.replace(r"\N", " ") for page in pages), text)

    def test_pagination_balances_four_lines_as_two_plus_two(self) -> None:
        pages = paginate_sentence("aaaa bbbb cccc dddd", width=4, max_lines=3)
        self.assertEqual([len(page.split(r"\N")) for page in pages], [2, 2])

    def test_video_cannot_finish_before_cues(self) -> None:
        with self.assertRaisesRegex(ValueError, "before the final"):
            generate_ass(
                [SubtitleCue(0.0, 3.0, "Still speaking.")],
                platform="App",
                code="1",
                video_duration=2.0,
            )


if __name__ == "__main__":
    unittest.main()
