from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from storyforge.services.media import (
    COLOR_GRADE_FILTERS,
    DEFAULT_USAGE_FILENAME,
    MediaError,
    MusicPlan,
    VIDEO_FADE_SECONDS,
    VideoSegment,
    build_ffmpeg_plan,
    build_low_memory_segment_plan,
    clear_duration_cache,
    escape_filter_path,
    execute_ffmpeg,
    load_usage_record,
    plan_video_segments,
    probe_duration,
    select_music_asset,
    select_video_assets,
    summarize_video_usage,
)


class MediaSelectionTests(unittest.TestCase):
    def test_filter_path_preserves_apostrophe_across_ffmpeg_parser_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            subtitle = (
                Path(temporary_directory)
                / "Second Chance at Love_ The Billionaire's Bride"
                / "preview subtitles.ass"
            )

            escaped = escape_filter_path(subtitle)

            self.assertIn(r"Billionaire'\\\''s Bride", escaped)
            self.assertIn(r"\:", escaped)
            self.assertNotIn(r"Billionaire\'s Bride", escaped)

    def test_least_used_video_order_and_supported_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "A clip.MP4").touch()
            (root / "b.mov").touch()
            (root / "nested").mkdir()
            (root / "nested" / "c.WEBM").touch()
            (root / "ignored.avi").touch()
            (root / DEFAULT_USAGE_FILENAME).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "usage": {
                            "a clip.mp4": 5,
                            "b.mov": 0,
                            "nested/c.webm": 2,
                        },
                    }
                ),
                encoding="utf-8",
            )

            assets = select_video_assets(root)

            self.assertEqual([asset.path.name for asset in assets], ["b.mov", "c.WEBM", "A clip.MP4"])
            self.assertEqual([asset.usage_count for asset in assets], [0, 2, 5])

    def test_segment_plan_uses_every_distinct_asset_before_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for name in ("a.mp4", "b.mov", "c.mkv"):
                (root / name).touch()
            (root / DEFAULT_USAGE_FILENAME).write_text(
                json.dumps({"version": 1, "usage": {"a.mp4": 2}}), encoding="utf-8"
            )
            durations = {"a.mp4": 9.0, "b.mov": 7.0, "c.mkv": 5.0}

            segments = plan_video_segments(
                root,
                24.0,
                duration_resolver=lambda path: durations[path.name],
            )

            self.assertEqual(
                [segment.path.name for segment in segments],
                ["b.mov", "c.mkv", "a.mp4", "b.mov"],
            )
            self.assertEqual(
                [segment.duration for segment in segments],
                [7.0, 5.0, 9.0, 3.0],
            )
            self.assertEqual(
                [segment.mirror for segment in segments],
                [False, False, False, True],
            )
            self.assertEqual(
                [segment.start_time for segment in segments],
                [0.0, 0.0, 0.0, 0.0],
            )
            self.assertEqual(
                [segment.speed for segment in segments],
                [1.0, 1.0, 1.0, 1.0],
            )
            self.assertEqual(
                [segment.crop_scale for segment in segments],
                [1.0, 1.0, 1.0, 1.0],
            )
            self.assertAlmostEqual(sum(segment.duration for segment in segments), 24.0)
            usage = load_usage_record(root)
            self.assertEqual(usage["a.mp4"], 3)
            self.assertEqual(usage["b.mov"], 2)
            self.assertEqual(usage["c.mkv"], 1)

    def test_single_long_enough_asset_wins_before_shorter_lower_usage_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for name in ("short.mp4", "long-used.mp4", "long-fresher.mp4"):
                (root / name).touch()
            (root / DEFAULT_USAGE_FILENAME).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "usage": {
                            "short.mp4": 0,
                            "long-used.mp4": 4,
                            "long-fresher.mp4": 2,
                        },
                    }
                ),
                encoding="utf-8",
            )
            durations = {
                "short.mp4": 8.0,
                "long-used.mp4": 30.0,
                "long-fresher.mp4": 20.0,
            }

            segments = plan_video_segments(
                root,
                10.0,
                duration_resolver=lambda path: durations[path.name],
                commit_usage=False,
            )

            self.assertEqual(len(segments), 1)
            self.assertEqual(segments[0].path.name, "long-fresher.mp4")
            self.assertEqual(segments[0].duration, 10.0)
            self.assertEqual(segments[0].usage_count_before, 2)

    def test_distinct_assets_are_spliced_without_reuse_when_total_is_enough(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for name in ("a.mp4", "b.mp4", "c.mp4"):
                (root / name).touch()

            segments = plan_video_segments(
                root,
                10.0,
                duration_resolver=lambda _path: 4.0,
                commit_usage=False,
            )

            self.assertEqual(
                [segment.path.name for segment in segments],
                ["a.mp4", "b.mp4", "c.mp4"],
            )
            self.assertEqual([segment.duration for segment in segments], [4.0, 4.0, 2.0])
            self.assertEqual(len({segment.path for segment in segments}), 3)

    def test_segment_plan_can_be_previewed_without_changing_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "clip.mp4").touch()
            plan_video_segments(
                root,
                2.0,
                duration_resolver=lambda _path: 10.0,
                commit_usage=False,
            )
            self.assertFalse((root / DEFAULT_USAGE_FILENAME).exists())

    def test_variant_seed_is_reproducible_without_changing_fixed_speed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for name in ("a.mp4", "b.mp4", "c.mp4"):
                (root / name).write_bytes(name.encode("ascii"))
            durations = {name: 30.0 for name in ("a.mp4", "b.mp4", "c.mp4")}

            def resolve(path: Path) -> float:
                return durations[path.name]

            first = plan_video_segments(
                root,
                6.0,
                duration_resolver=resolve,
                commit_usage=False,
                variant_seed=1,
                playback_speed=1.75,
            )
            repeated = plan_video_segments(
                root,
                6.0,
                duration_resolver=resolve,
                commit_usage=False,
                variant_seed=1,
                playback_speed=1.75,
            )
            second = plan_video_segments(
                root,
                6.0,
                duration_resolver=resolve,
                commit_usage=False,
                variant_seed=2,
                playback_speed=1.75,
            )

            self.assertEqual(first, repeated)
            self.assertNotEqual(first, second)
            self.assertNotEqual(first[0].path, second[0].path)
            self.assertGreater(first[0].start_time, 0.0)
            self.assertGreater(second[0].start_time, 0.0)
            self.assertNotEqual(first[0].mirror, second[0].mirror)
            self.assertNotEqual(first[0].crop_scale, second[0].crop_scale)
            for segment in (*first, *second):
                self.assertEqual(segment.speed, 1.75)
                self.assertGreaterEqual(segment.crop_scale, 1.0)
                self.assertLessEqual(segment.crop_scale, 1.05)
            self.assertTrue(any(segment.crop_scale > 1.0 for segment in (*first, *second)))

    def test_fixed_playback_speed_applies_to_every_planned_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for name in ("a.mp4", "b.mp4"):
                (root / name).touch()

            segments = plan_video_segments(
                root,
                8.0,
                duration_resolver=lambda _path: 6.0,
                commit_usage=False,
                variant_seed=8,
                playback_speed=2.0,
            )

            self.assertGreater(len(segments), 2)
            self.assertTrue(all(segment.speed == 2.0 for segment in segments))
            self.assertAlmostEqual(sum(segment.duration for segment in segments), 8.0)
            self.assertTrue(
                all(
                    segment.start_time + segment.source_span
                    <= segment.source_duration + 1e-6
                    for segment in segments
                )
            )

    def test_playback_speed_and_transition_contract_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "clip.mp4").touch()
            for speed in (0.8, 3.0):
                with self.subTest(accepted_speed=speed):
                    segments = plan_video_segments(
                        root,
                        5.0,
                        duration_resolver=lambda _path: 30.0,
                        commit_usage=False,
                        playback_speed=speed,
                    )
                    self.assertEqual(segments[0].speed, speed)
            for speed in (0.79, 3.01, float("nan"), True):
                with self.subTest(speed=speed):
                    with self.assertRaisesRegex(ValueError, "playback_speed"):
                        plan_video_segments(
                            root,
                            5.0,
                            duration_resolver=lambda _path: 30.0,
                            playback_speed=speed,
                        )
            with self.assertRaisesRegex(ValueError, "video_transition"):
                plan_video_segments(
                    root,
                    5.0,
                    duration_resolver=lambda _path: 30.0,
                    video_transition="dissolve",
                )

    def test_fade_planning_adds_overlap_but_keeps_effective_target_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for name in ("a.mp4", "b.mp4", "c.mp4"):
                (root / name).touch()

            segments = plan_video_segments(
                root,
                10.0,
                duration_resolver=lambda _path: 5.0,
                commit_usage=False,
                video_transition="fade",
            )

            self.assertEqual(
                [segment.duration for segment in segments[:2]],
                [5.0, 5.0],
            )
            self.assertAlmostEqual(segments[2].duration, 0.4)
            effective_duration = sum(segment.duration for segment in segments) - (
                VIDEO_FADE_SECONDS * (len(segments) - 1)
            )
            self.assertAlmostEqual(effective_duration, 10.0)
            self.assertEqual(len({segment.path for segment in segments}), 3)

    def test_usage_summary_lists_zero_and_nonzero_counts_without_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "A.MP4").touch()
            (root / "nested").mkdir()
            (root / "nested" / "b.mov").touch()
            (root / DEFAULT_USAGE_FILENAME).write_text(
                json.dumps({"version": 1, "usage": {"a.mp4": 4}}),
                encoding="utf-8",
            )

            summary = summarize_video_usage(root)

            self.assertEqual(summary, {"A.MP4": 4, "nested/b.mov": 0})

    def test_no_supported_video_is_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(MediaError, "No supported videos"):
                plan_video_segments(
                    temporary_directory,
                    10,
                    duration_resolver=lambda _path: 10,
                )

    def test_segment_plan_excludes_a_failed_asset_during_one_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            failed = root / "a-broken.mp4"
            replacement = root / "b-working.mp4"
            failed.touch()
            replacement.touch()

            segments = plan_video_segments(
                root,
                8.0,
                duration_resolver=lambda _path: 30.0,
                commit_usage=False,
                excluded_paths=(failed,),
            )

            self.assertEqual({segment.path for segment in segments}, {replacement})

    def test_segment_plan_reports_no_video_when_every_asset_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            only = root / "only.mp4"
            only.touch()

            with self.assertRaisesRegex(MediaError, "No supported videos"):
                plan_video_segments(
                    root,
                    8.0,
                    duration_resolver=lambda _path: 30.0,
                    commit_usage=False,
                    excluded_paths=(only,),
                )

    def test_category_mismatch_never_borrows_another_category(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            romance = root / "romance"
            romance.mkdir()
            (romance / "romance-only.mp4").touch()

            with self.assertRaisesRegex(MediaError, "category 'suspense'"):
                plan_video_segments(
                    root,
                    10,
                    mood="suspense",
                    duration_resolver=lambda _path: 30,
                    commit_usage=False,
                )


class MusicSelectionTests(unittest.TestCase):
    def test_least_used_track_wins_before_duration_then_path_break_ties(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            category = root / "romance"
            category.mkdir()
            for name in ("used-short.mp3", "z-unused-long.mp3", "a-unused-fit.mp3"):
                (category / name).touch()
            (root / DEFAULT_USAGE_FILENAME).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "usage": {
                            "romance/used-short.mp3": 5,
                            "romance/z-unused-long.mp3": 0,
                            "romance/a-unused-fit.mp3": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            durations = {
                "used-short.mp3": 91.0,
                "z-unused-long.mp3": 180.0,
                "a-unused-fit.mp3": 110.0,
            }

            plan = select_music_asset(
                root,
                "romance",
                90.0,
                duration_resolver=lambda path: durations[path.name],
            )

            self.assertEqual(plan.path.name, "a-unused-fit.mp3")
            self.assertEqual(plan.loops, 1)

    def test_chinese_category_and_long_enough_track_are_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            category = root / "浪漫"
            category.mkdir()
            (category / "short.mp3").touch()
            (category / "just right.wav").touch()
            (category / "very long.flac").touch()
            durations = {"short.mp3": 35.0, "just right.wav": 95.0, "very long.flac": 180.0}

            plan = select_music_asset(
                root,
                "romantic",
                90.0,
                duration_resolver=lambda path: durations[path.name],
            )

            self.assertEqual(plan.path.name, "just right.wav")
            self.assertEqual(plan.category, "romance")
            self.assertEqual(plan.loops, 1)

    def test_longest_short_track_is_looped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            category = root / "悬疑"
            category.mkdir()
            (category / "thirty.mp3").touch()
            (category / "fifty.mp3").touch()
            durations = {"thirty.mp3": 30.0, "fifty.mp3": 50.0}

            plan = select_music_asset(
                root,
                "suspense",
                125.0,
                duration_resolver=lambda path: durations[path.name],
            )

            self.assertEqual(plan.path.name, "fifty.mp3")
            self.assertEqual(plan.loops, 3)
            self.assertTrue(plan.needs_loop)

    def test_music_selection_excludes_a_failed_track_during_one_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            category = root / "romance"
            category.mkdir()
            failed = category / "a-broken.mp3"
            replacement = category / "b-working.mp3"
            failed.touch()
            replacement.touch()

            plan = select_music_asset(
                root,
                "romance",
                20.0,
                duration_resolver=lambda _path: 60.0,
                excluded_paths=(failed,),
            )

            self.assertEqual(plan.path, replacement)


class FFmpegPlanningTests(unittest.TestCase):
    def test_low_memory_segment_plan_opens_one_input_and_bounds_threads(self) -> None:
        base = Path("C:/Story Forge/low memory")
        segment = VideoSegment(
            base / "landscape.mp4",
            30.0,
            8.0,
            start_time=4.0,
            mirror=True,
            speed=1.5,
            source_width=1920,
            source_height=1080,
        )

        plan = build_low_memory_segment_plan(
            segment,
            base / "normalized.mp4",
            color_grade="romance_warm",
        )

        command = plan.as_list()
        self.assertEqual(command.count("-i"), 1)
        self.assertEqual(command[command.index("-filter_complex_threads") + 1], "1")
        self.assertEqual(command[command.index("-filter_threads") + 1], "1")
        self.assertEqual(command.count("-threads"), 2)
        self.assertIn("trim=start=4:duration=12", plan.filter_complex)
        self.assertIn("setpts=(PTS-STARTPTS)/1.5,hflip", plan.filter_complex)
        self.assertIn("gblur=sigma=10:steps=2", plan.filter_complex)
        self.assertIn(COLOR_GRADE_FILTERS["romance_warm"], plan.filter_complex)
        self.assertEqual(command[-1], str(base / "normalized.mp4"))

    def test_compatibility_render_bounds_filter_and_encoder_threads(self) -> None:
        base = Path("C:/Story Forge/compatibility")
        plan = build_ffmpeg_plan(
            [VideoSegment(base / "clip.mp4", 10.0, 10.0)],
            base / "voice.wav",
            None,
            base / "captions.ass",
            base / "output.mp4",
            10.0,
            render_mode="compatibility",
        )

        command = plan.as_list()
        self.assertEqual(command[command.index("-filter_threads") + 1], "1")
        self.assertEqual(command[command.index("-filter_complex_threads") + 1], "1")
        self.assertEqual(command[command.index("-threads") + 1], "2")

    def test_platform_logo_tracks_custom_intro_card_center_with_safe_clamp(self) -> None:
        base = Path("C:/Story Forge/custom-logo")
        logo = base / "logo-custom.png"
        plan = build_ffmpeg_plan(
            [VideoSegment(base / "video.mp4", 20.0, 20.0)],
            base / "voice.wav",
            MusicPlan(base / "music.mp3", 20.0, 1, "suspense"),
            base / "captions.ass",
            base / "output.mp4",
            20.0,
            platform_logo_path=logo,
            platform_logo_duration=5.5,
            platform_logo_x_percent=80,
            platform_logo_y_percent=45,
        )

        # A 64px logo requested at x=80% is clamped before the right-side
        # TikTok rail (1080 - 188 - 64 = 828); y follows the editable card.
        self.assertIn("[subtitled][platform_logo]overlay=828:864:", plan.filter_complex)

    def test_filter_graph_has_vertical_blur_subtitles_concat_and_ducking(self) -> None:
        base = Path("C:/Story Forge/assets with spaces")
        segments = [
            VideoSegment(base / "one.mp4", 12.0, 8.0),
            VideoSegment(base / "two.mov", 10.0, 4.0, mirror=True),
        ]
        music = MusicPlan(base / "music track.mp3", 30.0, 1, "suspense")

        plan = build_ffmpeg_plan(
            segments,
            base / "voice narration.wav",
            music,
            base / "captions file.ass",
            base / "output video.mp4",
            12.0,
        )

        command = plan.as_list()
        self.assertNotIn("shell=True", command)
        self.assertEqual(command[-1], str(base / "output video.mp4"))
        self.assertIn(str(base / "voice narration.wav"), command)
        self.assertIn("-stream_loop", command)
        self.assertIn("scale=360:640:force_original_aspect_ratio=increase", plan.filter_complex)
        self.assertIn("crop=360:640,gblur=sigma=10:steps=2", plan.filter_complex)
        self.assertIn("scale=1080:1920:flags=bilinear", plan.filter_complex)
        self.assertIn("force_original_aspect_ratio=decrease", plan.filter_complex)
        self.assertIn("concat=n=2:v=1:a=0", plan.filter_complex)
        self.assertIn("hflip", plan.filter_complex)
        self.assertIn("ass=filename=", plan.filter_complex)
        self.assertIn("sidechaincompress", plan.filter_complex)
        self.assertIn("amix=inputs=2", plan.filter_complex)
        self.assertIn("libx264", command)
        self.assertEqual(command[command.index("-r") + 1], "60")
        self.assertIn("fps=60", plan.filter_complex)
        # Normalise cadence only after the expensive blur, scaling and
        # compositing work.  A 30 fps source can therefore remain 30 fps
        # through those filters while the exported stream is still 60 fps.
        self.assertGreater(
            plan.filter_complex.index("fps=60"),
            plan.filter_complex.index("gblur=sigma=10:steps=2"),
        )
        self.assertGreater(
            plan.filter_complex.index("fps=60"),
            plan.filter_complex.index("overlay=(W-w)/2:(H-h)/2"),
        )
        self.assertIn(
            "fps=60,tpad=stop_mode=clone:stop_duration=0.033334,"
            "trim=duration=8,setpts=PTS-STARTPTS[v0]",
            plan.filter_complex,
        )
        self.assertIn('"C:\\Story Forge\\assets with spaces\\output video.mp4"', plan.readable_command)

    def test_fade_transition_uses_xfade_and_preserves_exact_target_duration(self) -> None:
        base = Path("C:/StoryForge")
        segments = [
            VideoSegment(base / "one.mp4", 5.0, 5.0),
            VideoSegment(base / "two.mp4", 5.0, 5.0),
            VideoSegment(base / "three.mp4", 2.0, 0.4),
        ]

        plan = build_ffmpeg_plan(
            segments,
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            10.0,
            video_transition="fade",
        )

        self.assertNotIn("concat=n=3", plan.filter_complex)
        self.assertIn(
            "[v0][v1]xfade=transition=fade:duration=0.2:offset=4.8,"
            "fps=60,settb=AVTB[xfade1]",
            plan.filter_complex,
        )
        self.assertIn(
            "[xfade1][v2]xfade=transition=fade:duration=0.2:offset=9.6,"
            "fps=60,settb=AVTB[xfade2]",
            plan.filter_complex,
        )
        self.assertIn(
            "[xfade2]trim=duration=10,setpts=PTS-STARTPTS[joined]",
            plan.filter_complex,
        )
        self.assertEqual(plan.duration, 10.0)
        self.assertEqual(plan.command[plan.command.index("-t") + 1], "10")

    def test_fade_transition_rejects_segments_that_do_not_cover_overlap(self) -> None:
        base = Path("C:/StoryForge")
        common = (
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            10.0,
        )
        with self.assertRaisesRegex(MediaError, "after fade transitions"):
            build_ffmpeg_plan(
                [
                    VideoSegment(base / "one.mp4", 5.0, 5.0),
                    VideoSegment(base / "two.mp4", 5.0, 5.0),
                ],
                *common,
                video_transition="fade",
            )
        with self.assertRaisesRegex(ValueError, "video_transition"):
            build_ffmpeg_plan(
                [VideoSegment(base / "one.mp4", 10.0, 10.0)],
                *common,
                video_transition="wipe",
            )

    def test_explicit_30_fps_is_preserved_in_filter_and_output(self) -> None:
        base = Path("C:/StoryForge")
        plan = build_ffmpeg_plan(
            [VideoSegment(base / "source.mp4", 8.0, 8.0)],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            8.0,
            fps=30,
        )

        command = plan.as_list()
        self.assertEqual(command[command.index("-r") + 1], "30")
        self.assertIn("fps=30", plan.filter_complex)

    def test_platform_logo_is_a_safe_intro_overlay_above_the_ass_card(self) -> None:
        base = Path("C:/Story Forge/品牌素材")
        logo = base / "GoodNovel logo.png"
        plan = build_ffmpeg_plan(
            [VideoSegment(base / "source.mp4", 12.0, 12.0)],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            12.0,
            platform_logo_path=logo,
            platform_logo_duration=5.5,
        )

        command = plan.as_list()
        logo_argument_index = command.index(str(logo))
        self.assertEqual(
            command[logo_argument_index - 5 : logo_argument_index],
            ["-loop", "1", "-framerate", "60", "-i"],
        )
        self.assertIn(
            "[3:v:0]fps=60,scale=64:64:force_original_aspect_ratio=decrease",
            plan.filter_complex,
        )
        self.assertIn("[joined]ass=filename=", plan.filter_complex)
        self.assertIn("[subtitled][platform_logo]overlay=508:550:", plan.filter_complex)
        self.assertIn("enable='between(t,0,5.5)'", plan.filter_complex)
        self.assertNotIn(str(logo), plan.filter_complex)

        preview = build_ffmpeg_plan(
            [VideoSegment(base / "source.mp4", 12.0, 12.0)],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "preview.mp4",
            12.0,
            width=540,
            height=960,
            platform_logo_path=logo,
            platform_logo_duration=5.5,
        )
        self.assertIn(
            "[3:v:0]fps=60,scale=32:32:force_original_aspect_ratio=decrease",
            preview.filter_complex,
        )
        # 254 + half the 32px logo box = the exact 270px frame centre.
        self.assertIn(
            "[subtitled][platform_logo]overlay=254:275:",
            preview.filter_complex,
        )

        with self.assertRaisesRegex(ValueError, "positive finite"):
            build_ffmpeg_plan(
                [VideoSegment(base / "source.mp4", 12.0, 12.0)],
                base / "voice.wav",
                base / "music.mp3",
                base / "captions.ass",
                base / "output.mp4",
                12.0,
                platform_logo_path=logo,
                platform_logo_duration=0,
            )

    def test_amf_uses_speed_first_quality_without_changing_output_geometry(self) -> None:
        base = Path("C:/StoryForge")
        segment = VideoSegment(base / "source.mp4", 12.0, 12.0)

        plan = build_ffmpeg_plan(
            [segment],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            12.0,
            video_encoder="h264_amf",
        )

        command = plan.as_list()
        quality_index = command.index("-quality")
        self.assertEqual(command[quality_index + 1], "speed")
        self.assertEqual(command[command.index("-qp_i") + 1], "22")
        self.assertEqual(command[command.index("-qp_p") + 1], "24")
        self.assertIn(
            "[fg0]scale=1080:1920:force_original_aspect_ratio=decrease[fgp0]",
            plan.filter_complex,
        )
        self.assertIn("[joined]ass=filename=", plan.filter_complex)

    def test_omitting_cover_preserves_the_original_plan_and_ignores_cover_timings(self) -> None:
        base = Path("C:/StoryForge")
        segment = VideoSegment(base / "source.mp4", 12.0, 12.0)
        arguments = (
            [segment],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            12.0,
        )

        original = build_ffmpeg_plan(*arguments)
        explicit_none = build_ffmpeg_plan(
            *arguments,
            cover_path=None,
            cover_intro_start=float("nan"),
            cover_intro_duration=-1.0,
            end_card_duration=999.0,
        )

        self.assertEqual(explicit_none.command, original.command)
        self.assertEqual(explicit_none.filter_complex, original.filter_complex)
        self.assertIn("[joined]ass=filename=", original.filter_complex)
        self.assertNotIn("cover_", original.filter_complex)
        self.assertNotIn("-loop", original.command)

    def test_cover_builds_seek_safe_intro_and_full_bleed_outro_below_ass(self) -> None:
        base = Path("C:/Story Forge/中文素材")
        cover = base / "小说封面 图.jpg"
        segments = [
            VideoSegment(base / "one.mp4", 10.0, 8.0),
            VideoSegment(base / "two.mp4", 10.0, 6.0),
        ]

        plan = build_ffmpeg_plan(
            segments,
            base / "旁白.wav",
            base / "背景音乐.mp3",
            base / "字幕.ass",
            base / "成片.mp4",
            14.0,
            cover_path=cover,
            cover_intro_start=2.0,
            cover_intro_duration=2.0,
            end_card_duration=6.0,
        )

        command = plan.as_list()
        cover_argument_index = command.index(str(cover))
        self.assertEqual(
            command[cover_argument_index - 5 : cover_argument_index],
            ["-loop", "1", "-framerate", "60", "-i"],
        )
        self.assertNotIn("trim=duration=14", plan.filter_complex)
        self.assertIn(
            "[4:v:0]fps=60,split=2"
            "[cover_intro_window_source][cover_end_window_source]",
            plan.filter_complex,
        )
        self.assertIn(
            "[cover_intro_window_source]trim=duration=2,"
            "setpts=PTS-STARTPTS+2/TB,split=2",
            plan.filter_complex,
        )
        self.assertIn(
            "[cover_end_window_source]trim=duration=6,"
            "setpts=PTS-STARTPTS+8/TB[cover_end_source]",
            plan.filter_complex,
        )
        self.assertNotIn(str(cover), plan.filter_complex)

        self.assertIn(
            "[cover_intro_bg_source]scale=360:640:force_original_aspect_ratio=increase,"
            "crop=360:640,gblur=sigma=10:steps=2,"
            "scale=1080:1920:flags=bilinear",
            plan.filter_complex,
        )
        self.assertIn(
            "[cover_intro_fg_source]scale=842:1574:"
            "force_original_aspect_ratio=decrease",
            plan.filter_complex,
        )
        self.assertIn("clip((t-2)/2,0,1)", plan.filter_complex)
        self.assertIn("clip((t-8)/6,0,1)", plan.filter_complex)
        self.assertGreaterEqual(plan.filter_complex.count("eval=frame"), 2)
        self.assertNotIn("zoompan", plan.filter_complex)
        self.assertIn("fade=t=in:st=2:d=0.35:alpha=1", plan.filter_complex)
        self.assertIn("fade=t=out:st=3.65:d=0.35:alpha=1", plan.filter_complex)
        self.assertIn("enable='between(t,2,4)'", plan.filter_complex)
        self.assertIn("fade=t=in:st=8:d=0.5:alpha=1", plan.filter_complex)
        self.assertIn("enable='gte(t,8)'", plan.filter_complex)
        self.assertIn("eof_action=pass:repeatlast=0", plan.filter_complex)
        self.assertIn(
            "[cover_end_source]scale=1080:1920:"
            "force_original_aspect_ratio=increase,crop=1080:1920",
            plan.filter_complex,
        )
        self.assertIn("crop=1080:1920:x='(iw-ow)/2':y='(ih-oh)/2'", plan.filter_complex)
        self.assertIn("overlay=0:0:enable='gte(t,8)'", plan.filter_complex)
        self.assertNotIn("[cover_end_bg][cover_end_fg]", plan.filter_complex)

        end_overlay_position = plan.filter_complex.index(
            "[cover_intro_composited][cover_end_scene]"
        )
        ass_position = plan.filter_complex.index("[covered]ass=filename=")
        self.assertLess(end_overlay_position, ass_position)
        self.assertNotIn("[joined]ass=filename=", plan.filter_complex)

    def test_disabled_cover_intro_keeps_only_the_full_bleed_cover_outro(self) -> None:
        base = Path("C:/StoryForge")
        cover = base / "cover.jpg"

        plan = build_ffmpeg_plan(
            [VideoSegment(base / "source.mp4", 14.0, 14.0)],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            14.0,
            cover_path=cover,
            cover_intro_enabled=False,
            # Disabled intro timings are deliberately ignored.
            cover_intro_start=float("nan"),
            cover_intro_duration=-1.0,
            end_card_duration=6.0,
        )

        self.assertIn(str(cover), plan.command)
        self.assertNotIn("cover_intro", plan.filter_complex)
        self.assertIn("[cover_end_source]", plan.filter_complex)
        self.assertIn(
            "[cover_end_window_source]trim=duration=6,"
            "setpts=PTS-STARTPTS+8/TB",
            plan.filter_complex,
        )
        self.assertIn("[cover_end_base]", plan.filter_complex)
        self.assertIn("[joined][cover_end_scene]", plan.filter_complex)
        self.assertIn("enable='gte(t,8)'", plan.filter_complex)
        self.assertIn("[covered]ass=filename=", plan.filter_complex)

    def test_disabled_cover_outro_keeps_intro_but_uses_caption_only_ending(self) -> None:
        base = Path("C:/StoryForge")
        cover = base / "cover.jpg"

        plan = build_ffmpeg_plan(
            [VideoSegment(base / "source.mp4", 14.0, 14.0)],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            14.0,
            cover_path=cover,
            cover_outro_enabled=False,
            end_card_duration=6.0,
            end_card_without_cover=True,
        )

        self.assertIn(str(cover), plan.command)
        self.assertIn("split=2[cover_intro_bg_source][cover_intro_fg_source]", plan.filter_complex)
        self.assertIn(
            "[cover_intro_window_source]trim=duration=2,"
            "setpts=PTS-STARTPTS+2/TB",
            plan.filter_complex,
        )
        self.assertIn("[cover_intro_composited]split=2", plan.filter_complex)
        self.assertNotIn("cover_end_source", plan.filter_complex)
        self.assertNotIn("cover_end_scene", plan.filter_complex)
        self.assertIn("eq=brightness=-0.24:saturation=0.72", plan.filter_complex)
        self.assertIn("[end_composited]ass=filename=", plan.filter_complex)
        self.assertIn("[voice_mix][ducked]amix=", plan.filter_complex)

    def test_caption_only_story_card_does_not_load_an_unused_cover(self) -> None:
        base = Path("C:/StoryForge")
        cover = base / "cover.jpg"

        plan = build_ffmpeg_plan(
            [VideoSegment(base / "source.mp4", 14.0, 14.0)],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            14.0,
            cover_path=cover,
            cover_intro_enabled=False,
            cover_outro_enabled=False,
            end_card_duration=6.0,
            end_card_without_cover=True,
        )

        self.assertNotIn(str(cover), plan.command)
        self.assertNotIn("cover_", plan.filter_complex)
        self.assertIn("[end_composited]ass=filename=", plan.filter_complex)
        self.assertIn("[voice_mix][ducked]amix=", plan.filter_complex)

        with self.assertRaisesRegex(ValueError, "cover_outro_enabled"):
            build_ffmpeg_plan(
                [VideoSegment(base / "source.mp4", 14.0, 14.0)],
                base / "voice.wav",
                base / "music.mp3",
                base / "captions.ass",
                base / "output.mp4",
                14.0,
                cover_outro_enabled="false",  # type: ignore[arg-type]
            )

    def test_no_cover_can_use_a_dedicated_darkened_end_card_below_ass(self) -> None:
        base = Path("C:/StoryForge")
        plan = build_ffmpeg_plan(
            [VideoSegment(base / "source.mp4", 14.0, 14.0)],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            14.0,
            end_card_duration=6.0,
            end_card_without_cover=True,
        )

        self.assertIn("[joined]split=2[story_main][story_end_source]", plan.filter_complex)
        self.assertIn(
            "[story_end_source]trim=start=8:duration=6,"
            "setpts=PTS-STARTPTS+8/TB",
            plan.filter_complex,
        )
        self.assertIn("eq=brightness=-0.24:saturation=0.72", plan.filter_complex)
        self.assertIn("enable='gte(t,8)'", plan.filter_complex)
        self.assertIn("eof_action=pass:repeatlast=0", plan.filter_complex)
        overlay_position = plan.filter_complex.index("[story_main][story_end_card]")
        ass_position = plan.filter_complex.index("[end_composited]ass=filename=")
        self.assertLess(overlay_position, ass_position)

    def test_cover_animation_modes_are_seek_safe_and_none_has_no_fades(self) -> None:
        base = Path("C:/StoryForge")
        arguments = (
            [VideoSegment(base / "source.mp4", 14.0, 14.0)],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            14.0,
        )
        static = build_ffmpeg_plan(
            *arguments,
            cover_path=base / "cover.jpg",
            cover_animation="none",
        )
        parallax = build_ffmpeg_plan(
            *arguments,
            cover_path=base / "cover.jpg",
            cover_animation="soft_parallax",
        )
        pull = build_ffmpeg_plan(
            *arguments,
            cover_path=base / "cover.jpg",
            cover_animation="gentle_pull",
        )
        pan = build_ffmpeg_plan(
            *arguments,
            cover_path=base / "cover.jpg",
            cover_animation="slow_pan",
        )

        self.assertNotIn("fade=t=", static.filter_complex)
        self.assertIn("eval=frame", static.filter_complex)
        self.assertIn("sin((t-2)*1.7)", parallax.filter_complex)
        self.assertIn("cos((t-2)*1.3)", parallax.filter_complex)
        self.assertIn("1.075-0.055*", pull.filter_complex)
        self.assertIn("(iw-ow)*(0.18+0.64*", pan.filter_complex)
        self.assertNotIn("zoompan", parallax.filter_complex)

    def test_curated_cover_animations_are_seek_safe_builtin_graphs(self) -> None:
        base = Path("C:/StoryForge")
        arguments = (
            [VideoSegment(base / "source.mp4", 14.0, 14.0)],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            14.0,
        )
        expected_fragments = {
            "vertical_drift": "(ih-oh)*(0.12+0.76*",
            "focus_reveal": "[cover_end_motion]split=2",
            "cinematic_push": "1+0.095*",
            "ken_burns_left": "(iw-ow)*(0.84-0.68*",
            "ken_burns_right": "(iw-ow)*(0.16+0.68*",
            "soft_flash": "color=white",
        }

        for animation, expected_fragment in expected_fragments.items():
            with self.subTest(animation=animation):
                plan = build_ffmpeg_plan(
                    *arguments,
                    cover_path=base / "cover.jpg",
                    cover_animation=animation,
                )
                self.assertIn(expected_fragment, plan.filter_complex)
                self.assertIn("eval=frame", plan.filter_complex)
                self.assertIn("clip((t-", plan.filter_complex)
                self.assertNotIn("zoompan", plan.filter_complex)

        focus = build_ffmpeg_plan(
            *arguments,
            cover_path=base / "cover.jpg",
            cover_animation="focus_reveal",
        )
        self.assertIn("gblur=sigma=9:steps=2", focus.filter_complex)
        self.assertIn("gblur=sigma=11:steps=2", focus.filter_complex)
        self.assertIn("fade=t=out:st=8:d=0.8:alpha=1", focus.filter_complex)

    def test_color_grade_whitelist_and_neutral_backward_compatibility(self) -> None:
        base = Path("C:/StoryForge")
        arguments = (
            [VideoSegment(base / "source.mp4", 8.0, 8.0)],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            8.0,
        )

        original = build_ffmpeg_plan(*arguments)
        explicit_neutral = build_ffmpeg_plan(*arguments, color_grade="neutral")
        self.assertEqual(explicit_neutral.command, original.command)
        self.assertEqual(explicit_neutral.filter_complex, original.filter_complex)

        for grade, grade_filter in COLOR_GRADE_FILTERS.items():
            if grade == "neutral":
                continue
            with self.subTest(grade=grade):
                plan = build_ffmpeg_plan(*arguments, color_grade=grade)
                self.assertIn(
                    f"format=yuv420p,{grade_filter},fps=60,",
                    plan.filter_complex,
                )

        cover_grade = "romance_warm"
        cover_filter = COLOR_GRADE_FILTERS[cover_grade]
        with_cover = build_ffmpeg_plan(
            *arguments,
            cover_path=base / "cover.jpg",
            color_grade=cover_grade,
            cover_intro_start=0,
            cover_intro_duration=1.5,
            end_card_duration=5,
        )
        # The UI calls this a full-video grade: normal footage, the animated
        # intro cover and the full-screen ending cover must all match.
        self.assertGreaterEqual(with_cover.filter_complex.count(cover_filter), 3)

        with self.assertRaisesRegex(ValueError, "color_grade must be"):
            build_ffmpeg_plan(*arguments, color_grade="unknown_downloaded_lut")

    def test_cover_timing_contract_rejects_unsafe_or_overlapping_windows(self) -> None:
        base = Path("C:/StoryForge")
        segment = VideoSegment(base / "source.mp4", 20.0, 20.0)
        common = (
            [segment],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
        )
        cases = [
            (14.0, {"cover_intro_start": -0.1}, "non-negative"),
            (14.0, {"cover_intro_duration": 1.49}, "between 1.5 and 2.5"),
            (14.0, {"cover_intro_duration": 2.51}, "between 1.5 and 2.5"),
            (14.0, {"end_card_duration": 4.99}, "between 5 and 7"),
            (14.0, {"end_card_duration": 7.01}, "between 5 and 7"),
            (5.0, {}, "cannot exceed"),
            (10.0, {"cover_intro_start": 3.0}, "finish before"),
            (14.0, {"cover_intro_start": float("inf")}, "must be finite"),
        ]

        for target_duration, options, message in cases:
            with self.subTest(target_duration=target_duration, options=options):
                with self.assertRaisesRegex(ValueError, message):
                    build_ffmpeg_plan(
                        *common,
                        target_duration,
                        cover_path=base / "cover.jpg",
                        **options,
                    )

    def test_audio_mix_uses_configured_music_level_and_gentle_ducking(self) -> None:
        base = Path("C:/StoryForge")
        segment = VideoSegment(base / "source.mp4", 12.0, 12.0)

        plan = build_ffmpeg_plan(
            [segment],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            12.0,
            bgm_volume=0.37,
        )

        expected_audio_graph = (
            "[1:a:0]aresample=48000,asetpts=PTS-STARTPTS,"
            "apad=whole_dur=12,atrim=0:12,asplit=2[voice_sc][voice_mix];"
            "[2:a:0]aresample=48000,asetpts=PTS-STARTPTS,"
            "atrim=0:12,volume=0.37[bgm];"
            "[bgm][voice_sc]sidechaincompress=threshold=0.05:ratio=4:"
            "attack=20:release=250:makeup=1[ducked];"
            "[voice_mix][ducked]amix=inputs=2:duration=first:"
            "dropout_transition=2:normalize=0,alimiter=limit=0.95[aout]"
        )
        self.assertIn(expected_audio_graph, plan.filter_complex)
        self.assertNotIn("threshold=0.025:ratio=10", plan.filter_complex)

    def test_none_music_omits_bgm_input_and_keeps_narration_only(self) -> None:
        base = Path("C:/StoryForge")
        plan = build_ffmpeg_plan(
            [VideoSegment(base / "source.mp4", 12.0, 12.0)],
            base / "voice.wav",
            None,
            base / "captions.ass",
            base / "output.mp4",
            12.0,
        )

        command = plan.as_list()
        self.assertNotIn("-stream_loop", command)
        self.assertEqual(command.count("-i"), 2)
        self.assertIn(
            "[1:a:0]aresample=48000,asetpts=PTS-STARTPTS,"
            "apad=whole_dur=12,atrim=0:12[aout]",
            plan.filter_complex,
        )
        self.assertNotIn("[bgm]", plan.filter_complex)
        self.assertNotIn("sidechaincompress", plan.filter_complex)
        self.assertNotIn("amix=", plan.filter_complex)
        self.assertNotIn("asplit=2[voice_sc][voice_mix]", plan.filter_complex)

    def test_none_music_keeps_optional_image_input_indexes_contiguous(self) -> None:
        base = Path("C:/StoryForge")
        plan = build_ffmpeg_plan(
            [VideoSegment(base / "source.mp4", 12.0, 12.0)],
            base / "voice.wav",
            None,
            base / "captions.ass",
            base / "output.mp4",
            12.0,
            cover_path=base / "cover.jpg",
            platform_logo_path=base / "logo.png",
        )

        self.assertIn("[2:v:0]fps=60,split=2", plan.filter_complex)
        self.assertIn("[3:v:0]fps=60,scale=64:64", plan.filter_complex)
        self.assertEqual(plan.command.count("-i"), 4)

    def test_variant_start_speed_and_crop_are_applied_without_geometry_changes(self) -> None:
        base = Path("C:/StoryForge")
        segment = VideoSegment(
            base / "source.mp4",
            source_duration=10.0,
            duration=5.0,
            start_time=2.0,
            mirror=True,
            speed=1.03,
            crop_scale=1.05,
        )

        plan = build_ffmpeg_plan(
            [segment],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            5.0,
        )

        self.assertIn("trim=start=2:duration=5.15", plan.filter_complex)
        self.assertIn("setpts=(PTS-STARTPTS)/1.03", plan.filter_complex)
        self.assertIn("hflip", plan.filter_complex)
        self.assertIn(
            "[fg0]scale=1134:2016:force_original_aspect_ratio=decrease[fgp0]",
            plan.filter_complex,
        )
        self.assertIn("scale=1080:1920:flags=bilinear", plan.filter_complex)
        self.assertIn("overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p", plan.filter_complex)

    def test_known_vertical_geometry_bypasses_fully_hidden_blurred_background(self) -> None:
        base = Path("C:/StoryForge")
        segment = VideoSegment(
            base / "vertical.mp4",
            source_duration=12.0,
            duration=12.0,
            source_width=1080,
            source_height=1920,
        )

        plan = build_ffmpeg_plan(
            [segment],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            12.0,
        )

        self.assertNotIn("[bg0]", plan.filter_complex)
        self.assertNotIn("gblur=", plan.filter_complex)
        self.assertIn(
            "scale=1080:1920:force_original_aspect_ratio=decrease,"
            "crop=1080:1920:x='(iw-ow)/2':y='(ih-oh)/2',"
            "setsar=1,format=yuv420p,fps=60,"
            "tpad=stop_mode=clone:stop_duration=0.033334,"
            "trim=duration=12,setpts=PTS-STARTPTS[v0]",
            plan.filter_complex,
        )

    def test_geometry_bypass_is_safe_for_unknown_and_letterboxed_sources(self) -> None:
        base = Path("C:/StoryForge")
        common = (
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            12.0,
        )
        unknown = build_ffmpeg_plan(
            [VideoSegment(base / "unknown.mp4", 12.0, 12.0)],
            *common,
        )
        landscape = build_ffmpeg_plan(
            [
                VideoSegment(
                    base / "landscape.mp4",
                    12.0,
                    12.0,
                    source_width=1920,
                    source_height=1080,
                )
            ],
            *common,
        )

        self.assertIn("[bg0]", unknown.filter_complex)
        self.assertIn("gblur=sigma=10:steps=2", unknown.filter_complex)
        self.assertIn("[bg0]", landscape.filter_complex)
        self.assertIn("gblur=sigma=10:steps=2", landscape.filter_complex)

    def test_crop_overscan_can_safely_cover_a_near_vertical_source(self) -> None:
        base = Path("C:/StoryForge")
        segment = VideoSegment(
            base / "near-vertical.mp4",
            source_duration=12.0,
            duration=12.0,
            crop_scale=1.025,
            source_width=1060,
            source_height=1920,
        )

        plan = build_ffmpeg_plan(
            [segment],
            base / "voice.wav",
            base / "music.mp3",
            base / "captions.ass",
            base / "output.mp4",
            12.0,
        )

        self.assertNotIn("[bg0]", plan.filter_complex)
        self.assertIn("scale=1108:1968:force_original_aspect_ratio=decrease", plan.filter_complex)

    def test_segment_geometry_must_be_complete_and_positive(self) -> None:
        base = Path("C:/StoryForge")
        with self.assertRaisesRegex(ValueError, "provided together"):
            VideoSegment(
                base / "source.mp4",
                12.0,
                12.0,
                source_width=1080,
            )
        for width, height in ((0, 1920), (1080, 0), (float("nan"), 1920)):
            with self.subTest(width=width, height=height):
                with self.assertRaisesRegex(ValueError, "positive finite"):
                    VideoSegment(
                        base / "source.mp4",
                        12.0,
                        12.0,
                        source_width=width,
                        source_height=height,
                    )

    def test_paths_are_individual_arguments_and_execute_disables_shell(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0)

        execute_ffmpeg(["ffmpeg", "-i", "C:/path with spaces/video.mp4"], runner=fake_runner)

        self.assertEqual(calls[0][0][2], "C:/path with spaces/video.mp4")
        self.assertIs(calls[0][1]["shell"], False)
        self.assertIs(calls[0][1]["check"], True)

    def test_probe_uses_argument_list_without_shell(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout="12.5\n", stderr="")

        duration = probe_duration("C:/path with spaces/video.mp4", runner=fake_runner)

        self.assertEqual(duration, 12.5)
        self.assertEqual(calls[0][0][-1], str(Path("C:/path with spaces/video.mp4")))
        self.assertIs(calls[0][1]["shell"], False)

    def test_duration_cache_is_thread_safe_and_invalidates_on_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "clip.mp4"
            path.write_bytes(b"one")
            calls: list[list[str]] = []

            def fake_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                time.sleep(0.02)
                return subprocess.CompletedProcess(command, 0, stdout="12.5\n", stderr="")

            clear_duration_cache()
            try:
                with ThreadPoolExecutor(max_workers=6) as executor:
                    durations = list(
                        executor.map(
                            lambda _index: probe_duration(path, runner=fake_runner),
                            range(6),
                        )
                    )
                self.assertEqual(durations, [12.5] * 6)
                self.assertEqual(len(calls), 1)

                path.write_bytes(b"changed-size")
                self.assertEqual(probe_duration(path, runner=fake_runner), 12.5)
                self.assertEqual(len(calls), 2)
            finally:
                clear_duration_cache()


if __name__ == "__main__":
    unittest.main()
