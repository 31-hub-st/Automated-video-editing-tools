from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest import mock

from storyforge.cancellation import (
    CancellationToken,
    JobCancelledError,
    cancellation_scope,
)
from storyforge.models import AppSettings, JobStatus, PlatformProfile, RenderJob
from storyforge.pipeline import (
    CHAPTER_MARKER,
    NarrationUnit,
    PipelineError,
    PipelineRunner,
    UsageLedger,
    _clear_media_decode_cache,
    _decode_media_sample,
    _fit_preview_tts_to_duration,
    _copy_file_atomic,
    _completed_failure_code,
    _cover_outro_enabled,
    _FFmpegProgressTracker,
    _ffmpeg_progress_command,
    _ffmpeg_progress_seconds,
    _plan_category_video_segments,
    _preflight_output_directory,
    _preflight_workspace_directory,
    _required_output_bytes,
    _serial_fallback_segments,
    _should_retry_in_low_memory_mode,
    _should_retry_with_cpu,
    _story_card_final_label,
    _story_card_platform_logo,
    assemble_narration_wav,
    group_narration_units,
    job_workspace_directory,
    narration_speed_for_wpm,
    narration_units,
    production_output_mode,
)
from storyforge.providers.text import TextRequest, TextResult
from storyforge.providers.base import ProviderError, ProviderResponseError
from storyforge.providers.tts import SpeechSegment, TTSResult
from storyforge.services.media import (
    DEFAULT_USAGE_FILENAME,
    MediaError,
    MusicPlan,
    VideoSegment,
)
from storyforge.services.quality import QualityReport


def _write_wav(
    path: Path,
    duration: float,
    *,
    rate: int = 1_000,
    channels: int = 1,
    sample_width: int = 2,
) -> None:
    frames = round(duration * rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(sample_width)
        stream.setframerate(rate)
        stream.writeframes(b"\x00" * frames * channels * sample_width)


def _speech_segment(index: int, text: str, path: Path, duration: float) -> SpeechSegment:
    return SpeechSegment(
        index=index,
        text=text,
        path=str(path),
        duration_seconds=duration,
        voice="af_bella",
        provider="fake-tts",
    )


class HeavyResourceSerializationTests(unittest.TestCase):
    def test_pipeline_holds_shared_heavy_resource_lock_for_entire_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock = threading.Lock()
            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                work_root=Path(temp) / "render-work",
                heavy_resource_lock=lock,
            )
            job = RenderJob(
                batch_id="batch-1",
                platform_id="platform-1",
                source_file=str(Path(temp) / "story.txt"),
                title="Story",
                code="B12345",
                video_folder=str(Path(temp) / "videos"),
                music_folder=str(Path(temp) / "music"),
                output_folder=str(Path(temp) / "output"),
            )
            platform = PlatformProfile(id="platform-1", name="GoodNovel")

            def assert_locked(*_args, **_kwargs):
                self.assertTrue(lock.locked())
                return "finished.mp4"

            with (
                mock.patch.object(runner, "_run_job", side_effect=assert_locked),
                mock.patch("storyforge.pipeline.release_embedded_kokoro_runtime"),
            ):
                result = runner(job, platform, lambda *_args: None)

            self.assertEqual(result, "finished.mp4")
            self.assertFalse(lock.locked())


class NarrationAssemblyTests(unittest.TestCase):
    def test_extreme_wpm_is_not_silently_capped_for_any_provider(self) -> None:
        self.assertAlmostEqual(
            narration_speed_for_wpm(280, "local_kokoro"), 280 / 185
        )
        self.assertAlmostEqual(
            narration_speed_for_wpm(280, "edge_tts"), 280 / 180
        )
        self.assertAlmostEqual(
            narration_speed_for_wpm(280, "deepgram"), 280 / 155
        )

    def test_storyforge_alignment_index_travels_inside_copied_mp3(self) -> None:
        metadata = {
            "schema_version": 1,
            "novel_id": "novel-1",
            "episode_ids": ["episode-1"],
            "promo_code": "B56826",
            "text": {"polished_text": "She opened the door."},
            "duration_seconds": 3.5,
            "cues": [
                {"start": 0.0, "end": 3.5, "text": "She opened the door."}
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "narration.mp3"
            copied = Path(temp) / "copied-to-another-computer.mp3"
            audio_payload = b"\xff\xfb" + (b"storyforge-audio" * 128)
            source.write_bytes(audio_payload)

            PipelineRunner._embed_narration_metadata(source, metadata)
            copied.write_bytes(source.read_bytes())

            self.assertTrue(copied.read_bytes().endswith(audio_payload))
            self.assertEqual(
                PipelineRunner._embedded_narration_metadata(copied), metadata
            )

    def test_output_preflight_writes_probe_and_checks_free_space(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "new-output"
            disk = mock.Mock(free=4 * 1024**3)
            with mock.patch(
                "storyforge.pipeline.shutil.disk_usage", return_value=disk
            ) as disk_usage:
                result = _preflight_output_directory(
                    output,
                    duration_seconds=600,
                    output_mode="video_and_mp3",
                )

            self.assertEqual(result, output.resolve())
            self.assertTrue(result.is_dir())
            self.assertEqual(list(result.glob(".storyforge-write-check-*.tmp")), [])
            disk_usage.assert_called_once_with(result)

    def test_output_preflight_rejects_an_unwritable_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            output.mkdir()
            with mock.patch(
                "storyforge.pipeline.tempfile.mkstemp",
                side_effect=PermissionError("denied"),
            ):
                with self.assertRaisesRegex(PipelineError, "输出文件夹不可写"):
                    _preflight_output_directory(
                        output,
                        duration_seconds=60,
                        output_mode="video_and_mp3",
                    )

    def test_output_preflight_reports_required_and_available_space(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            output.mkdir()
            required = _required_output_bytes(60, "audio_only")
            with mock.patch(
                "storyforge.pipeline.shutil.disk_usage",
                return_value=mock.Mock(free=required - 1),
            ):
                with self.assertRaisesRegex(
                    PipelineError,
                    "输出磁盘空间不足.*当前剩余.*本次至少需要",
                ):
                    _preflight_output_directory(
                        output,
                        duration_seconds=60,
                        output_mode="audio_only",
                    )

    def test_serial_output_preflight_reserves_peak_staging_space(self) -> None:
        normal = _required_output_bytes(600, "video_and_mp3")
        serial = _required_output_bytes(
            600,
            "video_and_mp3",
            serial_staging=True,
        )
        self.assertGreater(serial, normal)

    def test_workspace_preflight_protects_system_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "render-work"
            with mock.patch(
                "storyforge.pipeline.shutil.disk_usage",
                return_value=mock.Mock(free=16 * 1024**2),
            ):
                with self.assertRaisesRegex(PipelineError, "工作盘空间不足"):
                    _preflight_workspace_directory(
                        root,
                        duration_seconds=600,
                    )

    def test_completed_failure_code_includes_windows_resource_exit_codes(self) -> None:
        for return_code in (0xC0000017, 0xC000009A, 0xC000012D, -1073741523):
            with self.subTest(return_code=return_code):
                completed = subprocess.CompletedProcess(
                    ["ffmpeg"],
                    return_code,
                    stdout="",
                    stderr="",
                )
                self.assertEqual(
                    _completed_failure_code(completed, output_exists=False),
                    "resource_exhausted",
                )

    def test_cpu_retry_is_only_allowed_for_hardware_encoder_initialization(self) -> None:
        encoders = ["h264_nvenc", "libx264"]
        self.assertTrue(
            _should_retry_with_cpu("encoder_init", "h264_nvenc", encoders)
        )
        for code in (
            "unknown",
            "missing_input",
            "filter_or_subtitle",
            "permission_denied",
            "out_of_memory",
        ):
            with self.subTest(code=code):
                self.assertFalse(
                    _should_retry_with_cpu(code, "h264_nvenc", encoders)
                )

    def test_normalized_source_resource_failure_is_never_retried_with_same_graph(self) -> None:
        self.assertFalse(
            _should_retry_in_low_memory_mode(
                "out_of_memory",
                serial_render_prepared=True,
            )
        )
        self.assertTrue(
            _should_retry_in_low_memory_mode(
                "out_of_memory",
                serial_render_prepared=False,
            )
        )
        self.assertTrue(
            _should_retry_in_low_memory_mode(
                "resource_exhausted",
                serial_render_prepared=False,
            )
        )
        self.assertFalse(
            _should_retry_in_low_memory_mode(
                "unknown",
                serial_render_prepared=False,
            )
        )

    def test_legacy_output_checkbox_preserves_video_intent_without_overriding_new_mode(self) -> None:
        job = RenderJob(
            batch_id="batch",
            platform_id="platform",
            source_file="story.txt",
            title="Story",
            code="CODE",
            video_folder="",
            music_folder="",
            output_folder="output",
        )
        job.settings_snapshot = {"export_narration_audio": False}
        self.assertEqual(production_output_mode(job), "video_and_mp3")
        job.settings_snapshot = {"export_narration_audio": True}
        self.assertEqual(production_output_mode(job), "video_and_mp3")
        job.settings_snapshot = {
            "output_mode": "audio_only",
            "export_narration_audio": True,
        }
        self.assertEqual(production_output_mode(job), "audio_only")

    def test_compact_preview_uses_one_natural_body_clause_before_cta(self) -> None:
        result = TextResult(
            polished_text=(
                "Last night at ten o'clock, when I was getting ready to go to "
                "sleep, I got a call from an unknown number in our city."
            ),
            hook=(
                "Last night at ten o'clock, when I was getting ready to go to "
                "sleep, I got a call from an unknown number in our city."
            ),
            ending_cta="Download GoodNovel and search code B39760 to continue reading.",
            mood="suspense",
            provider="fake",
        )

        units = PipelineRunner._preview_units(
            result,
            preview_seconds=11.0,
            narration_wpm=225,
            include_ending=True,
            end_card_seconds=5.0,
        )

        spoken = [item.text for item in units if not item.is_chapter_break]
        self.assertEqual(len(spoken), 2)
        self.assertLessEqual(len(spoken[0].split()), 14)
        self.assertTrue(spoken[0].endswith("."))
        self.assertEqual(spoken[-1], result.ending_cta)

    def test_preview_fits_complete_body_groups_and_preserves_full_cta(self) -> None:
        units = [
            NarrationUnit("Opening sentence."),
            NarrationUnit("Second sentence."),
            NarrationUnit("This sentence would push the CTA past thirty seconds."),
            NarrationUnit(is_chapter_break=True),
            NarrationUnit("Download GoodNovel and search code B73165 to continue."),
        ]
        segments = (
            _speech_segment(1, units[0].text, Path("opening.wav"), 10.0),
            _speech_segment(2, units[1].text, Path("second.wav"), 10.0),
            _speech_segment(3, units[2].text, Path("third.wav"), 10.0),
            _speech_segment(4, units[4].text, Path("cta.wav"), 5.0),
        )

        fitted_result, fitted_units, fitted_counts = _fit_preview_tts_to_duration(
            TTSResult(segments, provider="fake-tts"),
            units,
            (1, 1, 1, 1),
            preview_seconds=30.0,
            chapter_pause_seconds=0.8,
        )

        self.assertEqual(
            [segment.text for segment in fitted_result.segments],
            [units[0].text, units[1].text, units[4].text],
        )
        self.assertEqual(
            [unit.text for unit in fitted_units if not unit.is_chapter_break],
            [units[0].text, units[1].text, units[4].text],
        )
        self.assertEqual(fitted_counts, (1, 1, 1))
        self.assertEqual(sum(unit.is_chapter_break for unit in fitted_units), 1)
        self.assertLessEqual(
            fitted_result.duration_seconds + 0.8,
            30.0,
        )

    def test_preview_fit_uses_last_break_for_cta_and_counts_body_chapter_pause(self) -> None:
        units = [
            NarrationUnit("Chapter one ending."),
            NarrationUnit(is_chapter_break=True),
            NarrationUnit("Chapter two opening."),
            NarrationUnit("This later sentence will not fit."),
            NarrationUnit(is_chapter_break=True),
            NarrationUnit("Download GoodNovel and search code B73165 to continue."),
        ]
        segments = (
            _speech_segment(1, units[0].text, Path("first.wav"), 10.0),
            _speech_segment(2, units[2].text, Path("second.wav"), 10.0),
            _speech_segment(3, units[3].text, Path("third.wav"), 10.0),
            _speech_segment(4, units[5].text, Path("cta.wav"), 5.0),
        )

        fitted_result, fitted_units, fitted_counts = _fit_preview_tts_to_duration(
            TTSResult(segments, provider="fake-tts"),
            units,
            (1, 1, 1, 1),
            preview_seconds=30.0,
            chapter_pause_seconds=0.8,
        )

        self.assertEqual(
            [segment.text for segment in fitted_result.segments],
            [units[0].text, units[2].text, units[5].text],
        )
        self.assertEqual(sum(unit.is_chapter_break for unit in fitted_units), 2)
        self.assertEqual(fitted_counts, (1, 1, 1))
        self.assertLessEqual(
            fitted_result.duration_seconds + 2 * 0.8,
            30.0,
        )

    def test_adjacent_sentences_share_natural_tts_chunks_but_keep_cues(self) -> None:
        units = [
            NarrationUnit("She opened the door."),
            NarrationUnit("Nobody was there."),
            NarrationUnit(is_chapter_break=True),
            NarrationUnit("Then her phone rang."),
        ]
        chunks, counts = group_narration_units(units)
        self.assertEqual(
            chunks,
            ["She opened the door. Nobody was there.", "Then her phone rang."],
        )
        self.assertEqual(counts, (2, 1))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.wav"
            second = root / "second.wav"
            _write_wav(first, 0.8)
            _write_wav(second, 0.4)
            result = TTSResult(
                (
                    _speech_segment(1, chunks[0], first, 0.8),
                    _speech_segment(2, chunks[1], second, 0.4),
                ),
                provider="fake",
            )
            assembled = assemble_narration_wav(
                result,
                units,
                root / "joined.wav",
                chapter_pause_seconds=0.2,
                segment_unit_counts=counts,
            )

        self.assertEqual([item.text for item in assembled.cues], [
            "She opened the door.",
            "Nobody was there.",
            "Then her phone rang.",
        ])
        self.assertAlmostEqual(assembled.duration_seconds, 1.4, places=3)
        self.assertAlmostEqual(assembled.cues[1].end, 0.8, places=3)
        self.assertAlmostEqual(assembled.cues[2].start, 1.0, places=3)

    def test_pcm_segments_are_joined_with_chapter_silence_and_exact_cues(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.wav"
            second = root / "second.wav"
            _write_wav(first, 0.10)
            _write_wav(second, 0.20)
            tts = TTSResult(
                segments=(
                    _speech_segment(1, "First sentence.", first, 99.0),
                    _speech_segment(2, "Second sentence.", second, 99.0),
                ),
                provider="fake-tts",
            )
            units = [
                NarrationUnit("First sentence."),
                NarrationUnit(is_chapter_break=True),
                NarrationUnit("Second sentence."),
            ]

            result = assemble_narration_wav(
                tts, units, root / "joined.wav", chapter_pause_seconds=0.15
            )

            self.assertAlmostEqual(result.duration_seconds, 0.45, places=6)
            self.assertEqual(len(result.cues), 2)
            self.assertAlmostEqual(result.cues[0].start, 0.0)
            self.assertAlmostEqual(result.cues[0].end, 0.10)
            self.assertAlmostEqual(result.cues[1].start, 0.25)
            self.assertAlmostEqual(result.cues[1].end, 0.45)
            with wave.open(str(result.path), "rb") as stream:
                self.assertEqual(stream.getnframes(), 450)
                self.assertEqual(stream.getframerate(), 1_000)

    def test_initial_silence_offsets_audio_and_subtitle_cues(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.wav"
            _write_wav(first, 0.20)
            tts = TTSResult(
                segments=(_speech_segment(1, "Opening sentence.", first, 0.20),),
                provider="fake-tts",
            )

            result = assemble_narration_wav(
                tts,
                [NarrationUnit("Opening sentence.")],
                root / "joined.wav",
                chapter_pause_seconds=0.0,
                initial_silence_seconds=0.30,
            )

            self.assertAlmostEqual(result.duration_seconds, 0.50, places=6)
            self.assertAlmostEqual(result.cues[0].start, 0.30, places=6)
            self.assertAlmostEqual(result.cues[0].end, 0.50, places=6)

    def test_sentence_count_mismatch_stops_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            only = root / "only.wav"
            _write_wav(only, 0.1)
            tts = TTSResult(
                segments=(_speech_segment(1, "Only.", only, 0.1),),
                provider="fake-tts",
            )
            output = root / "joined.wav"
            with self.assertRaisesRegex(PipelineError, "句数"):
                assemble_narration_wav(
                    tts,
                    [NarrationUnit("One."), NarrationUnit("Two.")],
                    output,
                    chapter_pause_seconds=0.1,
                )
            self.assertFalse(output.exists())

    def test_incompatible_segment_formats_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.wav"
            second = root / "second.wav"
            _write_wav(first, 0.1, rate=1_000)
            _write_wav(second, 0.1, rate=2_000)
            tts = TTSResult(
                segments=(
                    _speech_segment(1, "One.", first, 0.1),
                    _speech_segment(2, "Two.", second, 0.1),
                ),
                provider="fake-tts",
            )
            with self.assertRaisesRegex(PipelineError, "采样格式"):
                assemble_narration_wav(
                    tts,
                    [NarrationUnit("One."), NarrationUnit("Two.")],
                    root / "joined.wav",
                    chapter_pause_seconds=0.1,
                )

    def test_narration_units_keep_marker_as_silence_not_subtitle_text(self) -> None:
        result = TextResult(
            polished_text=f"First sentence. {CHAPTER_MARKER} Second sentence.",
            hook="A separate hook.",
            ending_cta="Search code 123456 to continue.",
            mood="suspense",
            provider="fake",
        )
        units = narration_units(result)
        self.assertEqual(sum(unit.is_chapter_break for unit in units), 1)
        self.assertNotIn(CHAPTER_MARKER, [unit.text for unit in units])
        self.assertEqual(
            [unit.text for unit in units if not unit.is_chapter_break],
            [
                "A separate hook.",
                "First sentence.",
                "Second sentence.",
                "Search code 123456 to continue.",
            ],
        )


class UsageLedgerTests(unittest.TestCase):
    def test_committed_usage_is_persisted_and_limit_checked_before_next_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = UsageLedger(Path(temp) / "usage.json")
            ledger.check("deepgram", 90, 100)
            ledger.commit("deepgram", 90)
            with self.assertRaisesRegex(PipelineError, "硬上限"):
                ledger.check("deepgram", 11, 100)
            reloaded = UsageLedger(Path(temp) / "usage.json")
            reloaded.check("deepgram", 10, 100)


class FFmpegProgressTests(unittest.TestCase):
    def test_progress_command_is_idempotent_and_keeps_output_last(self) -> None:
        command = ["ffmpeg.exe", "-i", "input.mp4", "output.mp4"]

        injected = _ffmpeg_progress_command(command)

        self.assertEqual(injected[-1], "output.mp4")
        self.assertEqual(injected[1:3], ["-progress", "pipe:1"])
        self.assertIn("-nostats", injected)
        self.assertEqual(_ffmpeg_progress_command(injected), injected)

    def test_progress_timestamps_accept_current_and_legacy_ffmpeg_fields(self) -> None:
        self.assertAlmostEqual(
            _ffmpeg_progress_seconds("out_time_us=1250000") or 0.0,
            1.25,
        )
        self.assertAlmostEqual(
            _ffmpeg_progress_seconds("out_time_ms=2500000") or 0.0,
            2.5,
        )
        self.assertAlmostEqual(
            _ffmpeg_progress_seconds("out_time=01:02:03.500") or 0.0,
            3723.5,
        )
        for line in ("out_time=N/A", "out_time_us=-1", "speed=1.0x", ""):
            with self.subTest(line=line):
                self.assertIsNone(_ffmpeg_progress_seconds(line))

    def test_progress_end_does_not_claim_success_before_process_exit(self) -> None:
        updates: list[tuple[float, str]] = []
        tracker = _FFmpegProgressTracker(
            lambda _status, value, label: updates.append((value, label))
        )
        attempt = tracker.begin_attempt(100.0, "hardware", minimum=0.68)
        attempt("frame=0")
        attempt("out_time_us=100000000")
        attempt("progress=end")

        self.assertAlmostEqual(tracker.value, 0.68)
        record = tracker.finish_attempt(
            succeeded=False,
            return_code=-12,
            failure_code="resource_exhausted",
        )

        self.assertAlmostEqual(tracker.value, 0.68)
        self.assertEqual(record["frames"], 0)
        self.assertEqual(record["return_code"], -12)
        self.assertTrue(any("失败" in label and "0 帧" in label for _, label in updates))

    def test_retry_starts_a_numbered_attempt_with_its_own_progress(self) -> None:
        updates: list[tuple[float, str]] = []
        tracker = _FFmpegProgressTracker(
            lambda _status, value, label: updates.append((value, label))
        )
        first = tracker.begin_attempt(100.0, "hardware", minimum=0.68)
        first("frame=120")
        first("out_time_us=50000000")
        first_record = tracker.finish_attempt(
            succeeded=False,
            return_code=1,
            failure_code="encoder_init",
            stage="ffmpeg_render",
        )
        first_value = tracker.value

        retry = tracker.begin_attempt(100.0, "cpu retry", minimum=0.74)
        retry_start = tracker.value
        retry("frame=0")
        retry("progress=end")
        second_record = tracker.finish_attempt(
            succeeded=False,
            return_code=-12,
            failure_code="resource_exhausted",
            stage="ffmpeg_cpu_fallback",
        )

        self.assertGreater(first_value, retry_start)
        self.assertAlmostEqual(retry_start, 0.74)
        self.assertAlmostEqual(tracker.value, 0.74)
        self.assertEqual(first_record["attempt"], 1)
        self.assertEqual(second_record["attempt"], 2)
        self.assertEqual(second_record["stage"], "ffmpeg_cpu_fallback")
        self.assertTrue(any("尝试 2" in label for _, label in updates))


class PipelineRunnerTests(unittest.TestCase):
    def test_single_clip_low_memory_source_skips_redundant_stitched_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staging = root / "output" / ".storyforge-staging" / "job"
            job_dir = root / "render-work" / "job"
            staging.mkdir(parents=True)
            job_dir.mkdir(parents=True)
            source = root / "one.mp4"
            source.write_bytes(b"one")
            commands: list[list[str]] = []

            def render(command, **_kwargs):
                command = list(command)
                commands.append(command)
                Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(command[-1]).write_bytes(b"video")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                command_runner=render,
                work_root=root / "render-work",
            )
            result, completed = runner._prepare_low_memory_video_source(
                segments=[
                    VideoSegment(
                        source,
                        8.0,
                        4.0,
                        source_width=1080,
                        source_height=1920,
                    )
                ],
                staging_dir=staging,
                job_dir=job_dir,
                width=1080,
                height=1920,
                fps=60,
                color_grade="neutral",
                video_transition="cut",
                progress=lambda *_args: None,
            )

            self.assertEqual(completed.returncode, 0)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(len(commands), 1)
            self.assertNotIn("concat", commands[0])
            self.assertEqual(result.path.name, "segment-0001.mp4")

    def test_low_memory_source_normalizes_one_clip_at_a_time_on_output_drive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staging = root / "output" / ".storyforge-staging" / "job"
            job_dir = root / "render-work" / "job"
            staging.mkdir(parents=True)
            job_dir.mkdir(parents=True)
            first = root / "one.mp4"
            second = root / "two.mp4"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            commands: list[list[str]] = []

            def render(command, **_kwargs):
                command = list(command)
                commands.append(command)
                Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(command[-1]).write_bytes(b"video")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                command_runner=render,
                work_root=root / "render-work",
            )
            result, completed = runner._prepare_low_memory_video_source(
                segments=[
                    VideoSegment(first, 8.0, 4.0, source_width=1080, source_height=1920),
                    VideoSegment(second, 8.0, 4.0, source_width=1080, source_height=1920),
                ],
                staging_dir=staging,
                job_dir=job_dir,
                width=1080,
                height=1920,
                fps=60,
                color_grade="neutral",
                video_transition="cut",
                progress=lambda *_args: None,
            )

            self.assertEqual(completed.returncode, 0)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertTrue(result.path.is_file())
            self.assertTrue(result.path.is_relative_to(staging))
            self.assertEqual(result.duration, 8.0)
            self.assertEqual(len(commands), 3)
            self.assertEqual(commands[0].count("-i"), 1)
            self.assertEqual(commands[1].count("-i"), 1)
            self.assertIn("concat", commands[2])
            self.assertTrue((job_dir / "render-command-low-memory.txt").is_file())

    def test_serial_fallback_replaces_fade_overlap_with_equal_length_cuts(self) -> None:
        segments = [
            VideoSegment(Path("one.mp4"), 10.0, 5.0),
            VideoSegment(Path("two.mp4"), 10.0, 5.0, speed=1.5),
            VideoSegment(Path("three.mp4"), 10.0, 2.0),
        ]

        serial = _serial_fallback_segments(segments, "fade")

        self.assertAlmostEqual(sum(item.duration for item in serial), 11.6)
        self.assertEqual(serial[0], segments[0])
        self.assertAlmostEqual(serial[1].duration, 4.8)
        self.assertAlmostEqual(serial[1].start_time, 0.3)
        self.assertAlmostEqual(serial[2].duration, 1.8)
        self.assertAlmostEqual(serial[2].start_time, 0.2)

    def test_oom_after_serial_normalization_does_not_repeat_full_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "123456_Story.txt"
            source.write_text("One complete sentence.", encoding="utf-8")
            for name in ("videos", "music", "output"):
                (root / name).mkdir()
            first = root / "videos" / "one.mp4"
            second = root / "videos" / "two.mp4"
            first.write_bytes(b"one")
            second.write_bytes(b"two")

            settings = AppSettings()
            settings.video_encoder = "auto"
            settings.bgm_mode = "none"
            settings.providers.text_provider = "local"
            settings.providers.tts_provider = "local_kokoro"

            class TextProvider:
                def polish(self, request):
                    return TextResult(
                        polished_text=request.text,
                        hook=request.text,
                        ending_cta="Search code 123456 to continue.",
                        mood="suspense",
                        provider="fake",
                    )

            class TTSProvider:
                def synthesize(self, sentences, output_dir, *, voice, speed, file_stem):
                    segments = []
                    for index, sentence in enumerate(sentences, start=1):
                        path = Path(output_dir) / f"{file_stem}-{index}.wav"
                        _write_wav(path, 0.05)
                        segments.append(_speech_segment(index, sentence, path, 0.05))
                    return TTSResult(tuple(segments), provider="fake")

            commands: list[list[str]] = []
            delivery_attempts = 0

            def render(command, **_kwargs):
                nonlocal delivery_attempts
                command = list(command)
                commands.append(command)
                output = Path(command[-1])
                if output.name.endswith(".partial.mp4"):
                    delivery_attempts += 1
                    if delivery_attempts == 1:
                        return subprocess.CompletedProcess(
                            command,
                            1,
                            stdout="",
                            stderr="[fc#0] Error while filtering: Cannot allocate memory",
                        )
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"media")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            def quality(_media_path, _expectation, **_kwargs):
                return QualityReport(
                    passed=True,
                    backend="fake",
                    elapsed_ms=1,
                    media={"video_codec": "h264", "width": 1080, "height": 1920},
                    checks=(),
                )

            runner = PipelineRunner(
                lambda: settings,
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                text_provider_factory=lambda _config: TextProvider(),
                tts_provider_factory=lambda _config: TTSProvider(),
                command_runner=render,
                quality_checker=quality,
                work_root=root / "render-work",
            )
            job = RenderJob(
                batch_id="batch",
                platform_id="platform",
                source_file=str(source),
                title="Story",
                code="123456",
                video_folder=str(root / "videos"),
                music_folder=str(root / "music"),
                output_folder=str(root / "output"),
            )

            def planned_segments(_folder, target, **_kwargs):
                first_duration = target / 2
                return [
                    VideoSegment(
                        first,
                        target,
                        first_duration,
                        source_width=1080,
                        source_height=1920,
                    ),
                    VideoSegment(
                        second,
                        target,
                        target - first_duration,
                        source_width=1080,
                        source_height=1920,
                    ),
                ]

            with (
                mock.patch(
                    "storyforge.pipeline.plan_video_segments",
                    side_effect=planned_segments,
                ),
                mock.patch(
                    "storyforge.pipeline.available_encoders",
                    return_value=["h264_nvenc", "libx264"],
                ),
                mock.patch(
                    "storyforge.pipeline._segments_with_geometry",
                    side_effect=lambda _ffmpeg, items: list(items),
                ),
                mock.patch("storyforge.pipeline._commit_video_usage"),
                mock.patch("storyforge.pipeline._commit_music_usage"),
            ):
                with self.assertRaisesRegex(PipelineError, "FFmpeg"):
                    runner(
                        job,
                        PlatformProfile(id="platform", name="NovelBox"),
                        lambda *_args: None,
                    )

            self.assertEqual(delivery_attempts, 1)
            job_dir = job_workspace_directory(job, runner.work_root)
            self.assertFalse((job_dir / "render-command-fallback.txt").exists())
            self.assertTrue((job_dir / "render-command-low-memory.txt").is_file())
            manifests = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifests["media"]["safe_serial_render"])
            self.assertEqual(manifests["result"]["status"], "failed")
            self.assertEqual(len(manifests["render_attempts"]), 1)
            self.assertEqual(
                manifests["render_attempts"][0]["stage"],
                "ffmpeg_render",
            )
            self.assertEqual(
                manifests["render_attempts"][0]["failure_code"],
                "out_of_memory",
            )
            delivery_commands = [
                item for item in commands if Path(item[-1]).name.endswith(".partial.mp4")
            ]
            self.assertEqual(len(delivery_commands), 1)
            self.assertTrue(all(command.count("-i") == 2 for command in delivery_commands))
            self.assertIn("h264_nvenc", delivery_commands[0])

    def test_media_decode_sample_is_cached_by_path_size_and_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ffmpeg = root / "ffmpeg.exe"
            media = root / "clip.mp4"
            ffmpeg.touch()
            media.write_bytes(b"first")
            commands: list[list[str]] = []

            def successful_decode(command, **_kwargs):
                commands.append(list(command))
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            _clear_media_decode_cache()
            first = _decode_media_sample(
                ffmpeg,
                media,
                "video",
                runner=successful_decode,
            )
            repeated = _decode_media_sample(
                ffmpeg,
                media,
                "video",
                runner=successful_decode,
            )
            media.write_bytes(b"changed-size")
            changed = _decode_media_sample(
                ffmpeg,
                media,
                "video",
                runner=successful_decode,
            )

            self.assertEqual(first, (True, ""))
            self.assertEqual(repeated, first)
            self.assertEqual(changed, first)
            self.assertEqual(len(commands), 2)
            self.assertIn("0:v:0", commands[0])
            self.assertIn("-xerror", commands[0])
            self.assertEqual(commands[0][-1], str(Path(commands[0][-1])))

    def test_media_decode_samples_video_start_middle_end_and_audio_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ffmpeg = root / "ffmpeg.exe"
            media = root / "asset.mp4"
            ffmpeg.touch()
            media.write_bytes(b"media")
            commands: list[list[str]] = []

            def successful_decode(command, **_kwargs):
                commands.append(list(command))
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            _clear_media_decode_cache()
            video_result = _decode_media_sample(
                ffmpeg,
                media,
                "video",
                duration_seconds=100.0,
                runner=successful_decode,
            )
            cached_video_result = _decode_media_sample(
                ffmpeg,
                media,
                "video",
                duration_seconds=100.0,
                runner=successful_decode,
            )
            audio_result = _decode_media_sample(
                ffmpeg,
                media,
                "audio",
                duration_seconds=100.0,
                runner=successful_decode,
            )

            self.assertEqual(video_result, (True, ""))
            self.assertEqual(cached_video_result, video_result)
            self.assertEqual(audio_result, (True, ""))
            self.assertEqual(len(commands), 5)
            video_commands = commands[:3]
            audio_commands = commands[3:]
            self.assertNotIn("-ss", video_commands[0])
            self.assertAlmostEqual(
                float(video_commands[1][video_commands[1].index("-ss") + 1]),
                49.825,
                places=3,
            )
            self.assertAlmostEqual(
                float(video_commands[2][video_commands[2].index("-ss") + 1]),
                99.65,
                places=3,
            )
            self.assertNotIn("-ss", audio_commands[0])
            self.assertAlmostEqual(
                float(audio_commands[1][audio_commands[1].index("-ss") + 1]),
                99.65,
                places=3,
            )

    def test_transient_decode_timeout_and_oserror_are_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ffmpeg = root / "ffmpeg.exe"
            media = root / "clip.mp4"
            ffmpeg.touch()
            media.write_bytes(b"media")
            calls = 0

            def transient_then_success(command, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise subprocess.TimeoutExpired(command, 20)
                if calls == 2:
                    raise OSError("temporary launch failure")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            _clear_media_decode_cache()
            timed_out = _decode_media_sample(
                ffmpeg,
                media,
                "video",
                runner=transient_then_success,
            )
            launch_failed = _decode_media_sample(
                ffmpeg,
                media,
                "video",
                runner=transient_then_success,
            )
            recovered = _decode_media_sample(
                ffmpeg,
                media,
                "video",
                runner=transient_then_success,
            )
            cached = _decode_media_sample(
                ffmpeg,
                media,
                "video",
                runner=transient_then_success,
            )

            self.assertFalse(timed_out[0])
            self.assertIn("暂时繁忙", timed_out[1])
            self.assertFalse(launch_failed[0])
            self.assertIn("无法启动", launch_failed[1])
            self.assertEqual(recovered, (True, ""))
            self.assertEqual(cached, recovered)
            self.assertEqual(calls, 3)

    def test_explicit_ffmpeg_decode_failure_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ffmpeg = root / "ffmpeg.exe"
            media = root / "broken.mp4"
            ffmpeg.touch()
            media.write_bytes(b"broken")
            calls = 0

            def failed_decode(command, **_kwargs):
                nonlocal calls
                calls += 1
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr="Invalid data found when processing input",
                )

            _clear_media_decode_cache()
            first = _decode_media_sample(
                ffmpeg,
                media,
                "video",
                duration_seconds=90.0,
                runner=failed_decode,
            )
            repeated = _decode_media_sample(
                ffmpeg,
                media,
                "video",
                duration_seconds=90.0,
                runner=failed_decode,
            )

            self.assertFalse(first[0])
            self.assertEqual(repeated, first)
            self.assertEqual(calls, 1)

    def test_undecodable_video_and_music_are_each_replaced_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ffmpeg = root / "ffmpeg.exe"
            ffmpeg.touch()
            video_category = root / "videos" / "romance"
            music_category = root / "music" / "romance"
            video_category.mkdir(parents=True)
            music_category.mkdir(parents=True)
            bad_video = video_category / "a-broken.mp4"
            good_video = video_category / "b-working.mp4"
            bad_music = music_category / "a-broken.mp3"
            good_music = music_category / "b-working.mp3"
            for path in (bad_video, good_video, bad_music, good_music):
                path.write_bytes(path.name.encode("utf-8"))

            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=ffmpeg,
                work_root=root / "work",
            )
            report: dict[str, object] = {}
            warnings: list[str] = []

            def sample(_ffmpeg, path, stream_kind, **_kwargs):
                self.assertIn(stream_kind, {"video", "audio"})
                return ("broken" not in Path(path).name, "synthetic decode error")

            with mock.patch(
                "storyforge.pipeline._decode_media_sample",
                side_effect=sample,
            ):
                segments, music = runner._ensure_decodable_media(
                    video_folder=str(root / "videos"),
                    music_folder=str(root / "music"),
                    mood="romance",
                    target_duration=12.0,
                    duration_resolver=lambda _path: 60.0,
                    variant_seed="retry-1",
                    segments=[VideoSegment(bad_video, 60.0, 12.0)],
                    music=MusicPlan(bad_music, 60.0, 1, "romance"),
                    selection_report=report,
                    warnings=warnings,
                )

            self.assertEqual({segment.path for segment in segments}, {good_video})
            self.assertEqual(music.path, good_music)
            self.assertEqual(report["decode_preflight"], "replaced")
            self.assertTrue(any("视频素材" in warning for warning in warnings))
            self.assertTrue(any("背景音乐" in warning for warning in warnings))

    def test_undecodable_video_without_replacement_has_clear_chinese_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ffmpeg = root / "ffmpeg.exe"
            ffmpeg.touch()
            video_category = root / "videos" / "romance"
            music_category = root / "music" / "romance"
            video_category.mkdir(parents=True)
            music_category.mkdir(parents=True)
            bad_video = video_category / "broken.mp4"
            good_music = music_category / "working.mp3"
            bad_video.write_bytes(b"broken")
            good_music.write_bytes(b"music")
            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=ffmpeg,
                work_root=root / "work",
            )

            def sample(_ffmpeg, path, _stream_kind, **_kwargs):
                return (Path(path) == good_music, "synthetic decode error")

            with (
                mock.patch(
                    "storyforge.pipeline._decode_media_sample",
                    side_effect=sample,
                ),
                self.assertRaisesRegex(PipelineError, "没有可替换的可用视频"),
            ):
                runner._ensure_decodable_media(
                    video_folder=str(root / "videos"),
                    music_folder=str(root / "music"),
                    mood="romance",
                    target_duration=12.0,
                    duration_resolver=lambda _path: 60.0,
                    variant_seed="retry-2",
                    segments=[VideoSegment(bad_video, 60.0, 12.0)],
                    music=MusicPlan(good_music, 60.0, 1, "romance"),
                    selection_report={},
                    warnings=[],
                )

    def test_broken_optional_cover_falls_back_to_caption_ending(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ffmpeg = root / "ffmpeg.exe"
            cover = root / "cover.jpg"
            ffmpeg.touch()
            cover.write_bytes(b"broken-cover")
            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=ffmpeg,
                work_root=root / "work",
            )
            warnings: list[str] = []

            with mock.patch(
                "storyforge.pipeline._decode_media_sample",
                return_value=(False, "invalid image"),
            ):
                result = runner._validated_optional_image(
                    cover,
                    asset_label="小说封面",
                    fallback_label="改用纯字幕结尾",
                    warnings=warnings,
                )

            self.assertIsNone(result)
            self.assertIn("改用纯字幕结尾", warnings[0])

    def test_missing_configured_cover_warns_before_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ffmpeg = root / "ffmpeg.exe"
            ffmpeg.touch()
            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=ffmpeg,
                work_root=root / "work",
            )
            warnings: list[str] = []

            result = runner._validated_optional_image(
                root / "missing-cover.jpg",
                asset_label="小说封面",
                fallback_label="改用纯字幕结尾",
                warnings=warnings,
            )

            self.assertIsNone(result)
            self.assertEqual(len(warnings), 1)
            self.assertIn("不存在或当前账号无权读取", warnings[0])
            self.assertIn("改用纯字幕结尾", warnings[0])

    def test_strict_hub_text_failure_is_not_retried_as_fake_local_fallback(self) -> None:
        settings = AppSettings()
        settings.providers.text_provider = "groq"
        settings.providers.allow_provider_fallback = True
        factory_calls: list[object] = []

        class StrictProvider:
            strict_quality = True

            def polish(self, _request):
                raise ProviderError(
                    "Hub AI unavailable",
                    provider="hub_text",
                    retryable=True,
                )

        def provider_factory(config):
            factory_calls.append(config)
            return StrictProvider()

        runner = PipelineRunner(
            lambda: settings,
            ffmpeg_path=Path("ffmpeg"),
            text_provider_factory=provider_factory,
        )
        warnings: list[str] = []

        with self.assertRaisesRegex(ProviderError, "Hub AI unavailable"):
            runner._polish(TextRequest(text="Keep the original quality."), settings, warnings)

        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(warnings, [])

    def test_atomic_narration_export_removes_partial_file_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "narration.wav"
            destination = root / "Story_narration.wav"
            source.write_bytes(b"complete-narration")

            def fail_after_partial_copy(_source, target, **_kwargs):
                target.write(b"partial")
                raise OSError("synthetic copy failure")

            with mock.patch(
                "storyforge.pipeline.shutil.copyfileobj",
                side_effect=fail_after_partial_copy,
            ):
                with self.assertRaisesRegex(OSError, "synthetic copy failure"):
                    _copy_file_atomic(source, destination)

            self.assertFalse(destination.exists())
            self.assertEqual(list(root.glob("*.partial")), [])

    def test_startup_recovery_rolls_back_a_hard_kill_between_pair_renames(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_root = root / "render-work"
            output_root = root / "output"
            publish_dir = output_root / "publish"
            publish_dir.mkdir(parents=True)
            final_video = publish_dir / "story.mp4"
            final_audio = publish_dir / "story.mp3"
            final_video.write_bytes(b"previous-video")
            final_audio.write_bytes(b"previous-audio")

            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                work_root=work_root,
            )
            staging_dir = output_root / ".storyforge-staging" / "job-hard-kill"
            staged_video = staging_dir / "story.partial.mp4"
            staged_audio = staging_dir / "story.partial.mp3"
            journal, transaction = runner._begin_publish_transaction(
                job_id="job-hard-kill",
                mode="video_and_mp3",
                staging_dir=staging_dir,
                artifacts=(
                    ("video", staged_video, final_video),
                    ("narration", staged_audio, final_audio),
                ),
            )
            self.assertIn(work_root.resolve(), journal.resolve().parents)
            self.assertNotIn(output_root.resolve(), journal.resolve().parents)
            staged_video.write_bytes(b"new-video")
            staged_audio.write_bytes(b"new-audio")
            runner._mark_publish_transaction_ready(journal, transaction)

            # Reproduce a process kill after the MP4 rename but before the MP3
            # rename and before the journal can be marked committed.
            for artifact in transaction["artifacts"]:
                Path(artifact["final"]).replace(Path(artifact["backup"]))
            staged_video.replace(final_video)
            self.assertTrue(final_video.is_file())
            self.assertFalse(final_audio.is_file())
            self.assertTrue(staged_audio.is_file())

            restarted = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                work_root=work_root,
            )

            self.assertEqual(final_video.read_bytes(), b"previous-video")
            self.assertEqual(final_audio.read_bytes(), b"previous-audio")
            self.assertFalse(staging_dir.exists())
            self.assertFalse(journal.exists())
            self.assertTrue(
                any(
                    "Rolled back incomplete publish transaction" in warning
                    for warning in restarted._publish_recovery_warnings
                )
            )

    def test_startup_recovery_deletes_an_orphan_without_a_previous_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_root = root / "app-data" / "render-work"
            output_root = root / "employee-output"
            final_video = output_root / "publish" / "story.mp4"
            final_audio = output_root / "publish" / "story.mp3"
            staging_dir = output_root / ".storyforge-staging" / "job-orphan"
            staged_video = staging_dir / "story.partial.mp4"
            staged_audio = staging_dir / "story.partial.mp3"
            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                work_root=work_root,
            )
            journal, transaction = runner._begin_publish_transaction(
                job_id="job-orphan",
                mode="video_and_mp3",
                staging_dir=staging_dir,
                artifacts=(
                    ("video", staged_video, final_video),
                    ("narration", staged_audio, final_audio),
                ),
            )
            staged_video.write_bytes(b"new-video")
            staged_audio.write_bytes(b"new-audio")
            runner._mark_publish_transaction_ready(journal, transaction)
            final_video.parent.mkdir(parents=True)
            staged_video.replace(final_video)

            PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                work_root=work_root,
            )

            self.assertFalse(final_video.exists())
            self.assertFalse(final_audio.exists())
            self.assertFalse(staging_dir.exists())
            self.assertFalse(journal.exists())

    def test_startup_recovery_keeps_journal_when_new_final_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_root = root / "app-data" / "render-work"
            output_root = root / "employee-output"
            final_video = output_root / "publish" / "story.mp4"
            final_audio = output_root / "publish" / "story.mp3"
            staging_dir = output_root / ".storyforge-staging" / "job-locked"
            staged_video = staging_dir / "story.partial.mp4"
            staged_audio = staging_dir / "story.partial.mp3"
            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                work_root=work_root,
            )
            journal, transaction = runner._begin_publish_transaction(
                job_id="job-locked",
                mode="video_and_mp3",
                staging_dir=staging_dir,
                artifacts=(
                    ("video", staged_video, final_video),
                    ("narration", staged_audio, final_audio),
                ),
            )
            staged_video.write_bytes(b"new-video")
            staged_audio.write_bytes(b"new-audio")
            runner._mark_publish_transaction_ready(journal, transaction)
            final_video.parent.mkdir(parents=True)
            staged_video.replace(final_video)

            original_unlink = Path.unlink

            def fail_for_locked_final(candidate: Path, *args, **kwargs):
                if candidate == final_video:
                    raise PermissionError("synthetic Windows media lock")
                return original_unlink(candidate, *args, **kwargs)

            with mock.patch.object(Path, "unlink", new=fail_for_locked_final):
                message = runner._recover_publish_transaction(journal)

            self.assertIn("Deferred rollback", message)
            self.assertTrue(final_video.is_file())
            self.assertFalse(final_audio.exists())
            self.assertTrue(staged_audio.is_file())
            self.assertTrue(staging_dir.is_dir())
            self.assertTrue(journal.is_file())

            # Once the external lock disappears, the preserved journal and
            # staged MP3 still contain everything needed to finish rollback.
            message = runner._recover_publish_transaction(journal)
            self.assertIn("Rolled back incomplete", message)
            self.assertFalse(final_video.exists())
            self.assertFalse(final_audio.exists())
            self.assertFalse(staging_dir.exists())
            self.assertFalse(journal.exists())

    def test_startup_recovery_keeps_evidence_when_new_final_signature_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_root = root / "app-data" / "render-work"
            output_root = root / "employee-output"
            final_video = output_root / "publish" / "story.mp4"
            final_audio = output_root / "publish" / "story.mp3"
            staging_dir = output_root / ".storyforge-staging" / "job-changed"
            staged_video = staging_dir / "story.partial.mp4"
            staged_audio = staging_dir / "story.partial.mp3"
            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                work_root=work_root,
            )
            journal, transaction = runner._begin_publish_transaction(
                job_id="job-changed",
                mode="video_and_mp3",
                staging_dir=staging_dir,
                artifacts=(
                    ("video", staged_video, final_video),
                    ("narration", staged_audio, final_audio),
                ),
            )
            staged_video.write_bytes(b"new-video")
            staged_audio.write_bytes(b"new-audio")
            runner._mark_publish_transaction_ready(journal, transaction)
            final_video.parent.mkdir(parents=True)
            staged_video.replace(final_video)
            transaction["state"] = "published_1"
            journal.write_text(
                json.dumps(transaction, ensure_ascii=False),
                encoding="utf-8",
            )

            # Model a sync/tagging process changing the exposed half-published
            # MP4 before StoryForge restarts.  It must not be silently deleted,
            # but the recovery journal and staged MP3 must not be discarded.
            final_video.write_bytes(b"externally-changed-video")
            message = runner._recover_publish_transaction(journal)

            self.assertIn("Deferred rollback", message)
            self.assertEqual(final_video.read_bytes(), b"externally-changed-video")
            self.assertFalse(final_audio.exists())
            self.assertTrue(staged_audio.is_file())
            self.assertTrue(staging_dir.is_dir())
            self.assertTrue(journal.is_file())

            with self.assertRaisesRegex(
                PipelineError, "previous publish transaction still requires recovery"
            ):
                runner._begin_publish_transaction(
                    job_id="job-changed",
                    mode="video_and_mp3",
                    staging_dir=staging_dir,
                    artifacts=(
                        ("video", staged_video, final_video),
                        ("narration", staged_audio, final_audio),
                    ),
                )
            self.assertEqual(final_video.read_bytes(), b"externally-changed-video")
            self.assertTrue(staged_audio.is_file())
            self.assertTrue(staging_dir.is_dir())
            self.assertTrue(journal.is_file())

    def test_startup_recovery_keeps_a_verified_committed_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_root = root / "app-data" / "render-work"
            output_root = root / "employee-output"
            final_video = output_root / "publish" / "story.mp4"
            final_audio = output_root / "publish" / "story.mp3"
            staging_dir = output_root / ".storyforge-staging" / "job-committed"
            staged_video = staging_dir / "story.partial.mp4"
            staged_audio = staging_dir / "story.partial.mp3"
            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                work_root=work_root,
            )
            journal, transaction = runner._begin_publish_transaction(
                job_id="job-committed",
                mode="video_and_mp3",
                staging_dir=staging_dir,
                artifacts=(
                    ("video", staged_video, final_video),
                    ("narration", staged_audio, final_audio),
                ),
            )
            staged_video.write_bytes(b"new-video")
            staged_audio.write_bytes(b"new-audio")
            runner._mark_publish_transaction_ready(journal, transaction)
            final_video.parent.mkdir(parents=True)
            staged_video.replace(final_video)
            staged_audio.replace(final_audio)
            transaction["state"] = "committed"
            journal.write_text(
                json.dumps(transaction, ensure_ascii=False),
                encoding="utf-8",
            )

            PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                work_root=work_root,
            )

            self.assertEqual(final_video.read_bytes(), b"new-video")
            self.assertEqual(final_audio.read_bytes(), b"new-audio")
            self.assertFalse(staging_dir.exists())
            self.assertFalse(journal.exists())

    def test_audio_only_publishes_mp3_without_touching_video_music_or_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "B73165_Story.txt"
            source.write_text("She opened the message and learned the truth.", encoding="utf-8")
            output_root = root / "output"
            output_root.mkdir()
            settings = AppSettings()
            settings.providers.text_provider = "local"
            settings.providers.tts_provider = "local_kokoro"

            class TextProvider:
                def polish(self, request):
                    return TextResult(
                        polished_text=request.text,
                        hook="The message changed everything.",
                        ending_cta="Search code B73165 to continue.",
                        mood="suspense",
                        provider="fake",
                    )

            class TTSProvider:
                def synthesize(self, sentences, output_dir, *, voice, speed, file_stem):
                    segments = []
                    for index, sentence in enumerate(sentences, start=1):
                        path = Path(output_dir) / f"{file_stem}-{index}.wav"
                        _write_wav(path, 0.05)
                        segments.append(_speech_segment(index, sentence, path, 0.05))
                    return TTSResult(tuple(segments), provider="fake")

            commands = []

            def encode_mp3(command, **_kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b"complete-mp3")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            runner = PipelineRunner(
                lambda: settings,
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                text_provider_factory=lambda _config: TextProvider(),
                tts_provider_factory=lambda _config: TTSProvider(),
                command_runner=encode_mp3,
                work_root=root / "render-work",
            )
            job = RenderJob(
                batch_id="batch-audio",
                platform_id="platform",
                source_file=str(source),
                title="Story",
                code="B73165",
                video_folder="",
                music_folder="",
                output_folder=str(output_root),
                settings_snapshot={"output_mode": "audio_only"},
            )
            platform = PlatformProfile(
                id="platform",
                name="NovelBox",
                search_template="Search {platform}: {code}",
                ending_template="Search {platform}: {code} to continue.",
            )

            with (
                mock.patch("storyforge.pipeline._plan_category_video_segments") as video_plan,
                mock.patch("storyforge.pipeline.select_music_asset") as music_plan,
                mock.patch("storyforge.pipeline.write_ass") as subtitle_writer,
            ):
                result = runner(job, platform, lambda *_args: None)

            result_path = Path(result)
            self.assertEqual(result_path.suffix, ".mp3")
            self.assertTrue(result_path.is_file())
            self.assertEqual(result_path, Path(job.narration_audio_file))
            self.assertEqual({path.suffix for path in result_path.parent.iterdir()}, {".mp3"})
            self.assertEqual(len(commands), 1)
            self.assertNotEqual(Path(commands[0][-1]), result_path)
            self.assertEqual(Path(commands[0][-1]).parent.parent.name, ".storyforge-staging")
            self.assertIn("libmp3lame", commands[0])
            video_plan.assert_not_called()
            music_plan.assert_not_called()
            subtitle_writer.assert_not_called()
            job_dir = job_workspace_directory(job, root / "render-work")
            self.assertFalse((job_dir / ".work" / "subtitles.ass").exists())
            self.assertFalse((job_dir / "render-command.txt").exists())
            manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["output_mode"], "audio_only")
            self.assertEqual(manifest["result"]["output_file"], str(result_path))

    def test_category_plan_uses_explicit_generic_fallback_without_false_match(self) -> None:
        generic = VideoSegment(
            path=Path("C:/local/videos/general/clip.mp4"),
            source_duration=60.0,
            duration=30.0,
            usage_count_before=2,
        )
        report = {}
        warnings = []
        with mock.patch(
            "storyforge.pipeline.plan_video_segments",
            side_effect=[
                MediaError("No supported videos for category 'suspense'"),
                [generic],
            ],
        ) as planner:
            segments = _plan_category_video_segments(
                "C:/local/videos",
                30.0,
                mood="suspense",
                duration_resolver=lambda _path: 30.0,
                variant_seed="job-1",
                selection_report=report,
                warnings=warnings,
            )

        self.assertEqual(segments, [generic])
        self.assertEqual(planner.call_count, 2)
        self.assertEqual(planner.call_args_list[0].kwargs["mood"], "suspense")
        self.assertIsNone(planner.call_args_list[1].kwargs["mood"])
        self.assertFalse(planner.call_args_list[1].kwargs["commit_usage"])
        self.assertEqual(report["mode"], "generic_fallback")
        self.assertTrue(report["fallback"])
        self.assertIsNone(report["matched_category"])
        self.assertEqual(report["source_scope"], "selected_root_recursive")
        self.assertEqual(report["max_usage_count_before"], 2)
        self.assertIn("通用素材回退", warnings[0])

    def test_category_and_generic_video_failure_remains_a_clear_error(self) -> None:
        with mock.patch(
            "storyforge.pipeline.plan_video_segments",
            side_effect=[
                MediaError("category empty"),
                MediaError("root empty"),
            ],
        ) as planner:
            with self.assertRaisesRegex(PipelineError, "总目录"):
                _plan_category_video_segments(
                    "C:/local/videos",
                    30.0,
                    mood="romance",
                    duration_resolver=lambda _path: 30.0,
                    variant_seed="job-2",
                )

        self.assertEqual(planner.call_count, 2)

    def test_video_selection_report_marks_in_job_reuse(self) -> None:
        clip = Path("C:/local/videos/general/clip.mp4")
        repeated = [
            VideoSegment(clip, 60.0, 20.0, usage_count_before=3),
            VideoSegment(
                clip,
                60.0,
                10.0,
                mirror=True,
                usage_count_before=4,
            ),
        ]
        report = {}
        warnings = []
        with mock.patch(
            "storyforge.pipeline.plan_video_segments",
            return_value=repeated,
        ):
            segments = _plan_category_video_segments(
                "C:/local/videos",
                30.0,
                mood="romance",
                duration_resolver=lambda _path: 60.0,
                variant_seed="job-3",
                selection_report=report,
                warnings=warnings,
            )

        self.assertEqual(segments, repeated)
        self.assertEqual(report["mode"], "category_match")
        self.assertEqual(report["matched_category"], "romance")
        self.assertEqual(report["unique_asset_count"], 1)
        self.assertEqual(report["repeated_segment_count"], 1)
        self.assertEqual(report["max_usage_count_before"], 4)
        self.assertIn("素材复用提示", warnings[0])

    def test_generic_fallback_recurses_employee_root_and_keeps_usage_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            suspense = root / "suspense" / "pack-a"
            general = root / "general" / "pack-b"
            suspense.mkdir(parents=True)
            general.mkdir(parents=True)
            used = suspense / "used.mp4"
            fresh = general / "fresh.mov"
            used.write_bytes(b"used")
            fresh.write_bytes(b"fresh")
            (root / DEFAULT_USAGE_FILENAME).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "usage": {
                            "suspense/pack-a/used.mp4": 8,
                            "general/pack-b/fresh.mov": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            report = {}
            warnings = []

            segments = _plan_category_video_segments(
                str(root),
                10.0,
                mood="romance",
                duration_resolver=lambda _path: 30.0,
                variant_seed="generic-job",
                selection_report=report,
                warnings=warnings,
            )

            self.assertEqual(segments[0].path, fresh)
            self.assertEqual(segments[0].usage_count_before, 0)
            self.assertEqual(report["mode"], "generic_fallback")
            self.assertIsNone(report["matched_category"])
            self.assertIn("通用素材回退", warnings[0])

    def test_locked_voice_does_not_hide_a_real_engine_failure(self) -> None:
        class BrokenProvider:
            def synthesize(self, *_args, **_kwargs):
                raise ProviderResponseError(
                    "Kokoro runtime could not initialize its logger.",
                    provider="local_kokoro",
                )

        with tempfile.TemporaryDirectory() as temp:
            settings = AppSettings()
            settings.providers.tts_provider = "local_kokoro"
            runner = PipelineRunner(
                lambda: settings,
                tts_provider_factory=lambda _config: BrokenProvider(),
            )

            with self.assertRaisesRegex(
                ProviderResponseError,
                "runtime could not initialize",
            ):
                runner._narrate(
                    ["A locked voice still needs honest diagnostics."],
                    Path(temp),
                    "confident",
                    "suspense",
                    settings,
                    [],
                    locked_voice_id="af_sarah",
                )

    def test_story_card_platform_logo_requires_a_real_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logo = Path(temp) / "platform logo.png"
            logo.write_bytes(b"not-decoded-in-this-helper")
            platform = PlatformProfile(name="GoodNovel", logo_path=str(logo))

            self.assertIsNone(_story_card_platform_logo(platform, False))
            self.assertEqual(
                _story_card_platform_logo(platform, True),
                str(logo.resolve()),
            )
            platform.logo_path = str(Path(temp) / "missing.png")
            self.assertIsNone(_story_card_platform_logo(platform, True))

    def test_story_card_final_label_is_safe_for_legacy_normal_and_final_jobs(self) -> None:
        required = {
            "batch_id": "batch",
            "platform_id": "platform",
            "source_file": __file__,
            "title": "Story",
            "code": "B73165",
            "video_folder": ".",
            "music_folder": ".",
            "output_folder": ".",
        }
        legacy = RenderJob(**required)
        normal = RenderJob(
            **required,
            episode_number=2,
            episode_count=5,
            is_final_episode=False,
        )
        final = RenderJob(
            **required,
            episode_number=5,
            episode_count=5,
            is_final_episode=True,
        )

        self.assertEqual(legacy.episode_count, 0)
        self.assertFalse(legacy.is_final_episode)
        self.assertEqual(legacy.to_dict()["episode_count"], 0)
        self.assertFalse(legacy.to_dict()["is_final_episode"])
        self.assertEqual(_story_card_final_label(legacy, True), "")
        self.assertEqual(_story_card_final_label(normal, True), "")
        self.assertEqual(_story_card_final_label(final, False), "")
        self.assertEqual(_story_card_final_label(final, True), "FINAL PART")

    def test_cover_outro_requires_both_frozen_setting_and_job_switch(self) -> None:
        required = {
            "batch_id": "batch",
            "platform_id": "platform",
            "source_file": __file__,
            "title": "Story",
            "code": "B73165",
            "video_folder": ".",
            "music_folder": ".",
            "output_folder": ".",
        }
        enabled = AppSettings(cover_outro_enabled=True)
        disabled = AppSettings(cover_outro_enabled=False)

        self.assertTrue(_cover_outro_enabled(enabled, RenderJob(**required)))
        self.assertFalse(
            _cover_outro_enabled(
                enabled,
                RenderJob(**required, cover_outro_enabled=False),
            )
        )
        self.assertFalse(_cover_outro_enabled(disabled, RenderJob(**required)))

    def test_approved_snapshot_freezes_creative_recipe_but_uses_live_secret(self) -> None:
        live = AppSettings(narration_wpm=230, bgm_volume=0.1)
        live.providers.text_api_key = "rotated-live-key"
        live.providers.text_provider = "groq"
        job = RenderJob(
            batch_id="batch",
            platform_id="platform",
            source_file=__file__,
            title="Story",
            code="B73165",
            video_folder=".",
            music_folder=".",
            output_folder=".",
            settings_snapshot={
                "narration_wpm": 205,
                "output_fps": 30,
                "bgm_volume": 0.31,
                "cover_outro_enabled": False,
                "providers": {"text_provider": "local", "text_model": "approved"},
            },
        )

        frozen = PipelineRunner._settings_for_job(live, job)

        self.assertEqual(frozen.narration_wpm, 205)
        self.assertEqual(frozen.output_fps, 30)
        self.assertEqual(frozen.bgm_volume, 0.31)
        self.assertFalse(frozen.cover_outro_enabled)
        self.assertEqual(frozen.providers.text_provider, "local")
        self.assertEqual(frozen.providers.text_model, "approved")
        self.assertEqual(frozen.providers.text_api_key, "rotated-live-key")

    def test_fake_providers_drive_complete_pipeline_and_preserve_chapter_pause(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            text_root = root / "texts"
            video_root = root / "videos"
            music_root = root / "music"
            output_root = root / "output"
            for folder in (text_root, video_root, music_root, output_root):
                folder.mkdir(parents=True)
            source = text_root / "123456_The Locked Door.txt"
            source.write_text(
                "Chapter 1\n"
                "At midnight, Ava heard the locked door open. She froze.\n\n"
                "Chapter 2\n"
                "The footsteps stopped behind her.",
                encoding="utf-8",
            )
            video_path = video_root / "calm.mp4"
            music_path = music_root / "romance.wav"
            video_path.write_bytes(b"placeholder")
            music_path.write_bytes(b"placeholder")

            settings = AppSettings(chapter_pause_seconds=0.08)
            settings.video_template = "platform_story_card"
            settings.export_narration_audio = True
            settings.providers.text_provider = "local"
            settings.providers.tts_provider = "local_kokoro"
            text_requests = []
            tts_calls = []

            class FakeTextProvider:
                def polish(self, request):
                    text_requests.append(request)
                    return TextResult(
                        polished_text=request.text,
                        hook="That locked door had never opened on its own.",
                        ending_cta=(
                            "Download NovelBox and search code 123456 to discover who was there."
                        ),
                        mood="romance",
                        provider="fake-text",
                        retention_ratio=1.0,
                    )

            class FakeTTSProvider:
                def synthesize(self, sentences, output_dir, *, voice, speed, file_stem):
                    tts_calls.append((list(sentences), voice, speed, file_stem))
                    segments = []
                    for index, sentence in enumerate(sentences, start=1):
                        path = Path(output_dir) / f"{file_stem}-{index:04d}.wav"
                        _write_wav(path, 0.05, rate=1_000)
                        segments.append(
                            _speech_segment(index, sentence, path, 0.05)
                        )
                    return TTSResult(tuple(segments), provider="fake-tts")

            text_configs = []
            tts_configs = []

            def text_factory(config):
                text_configs.append(config)
                return FakeTextProvider()

            def tts_factory(config):
                tts_configs.append(config)
                return FakeTTSProvider()

            commands = []
            quality_expectations = []

            def command_runner(command, **_kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b"rendered-video")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            def quality_checker(media_path, expectation, **_kwargs):
                quality_expectations.append((Path(media_path), expectation))
                if Path(media_path).name.endswith(".partial.mp4"):
                    publish = Path(job.publish_batch_folder)
                    formal_name = Path(media_path).name.removesuffix(".partial.mp4") + ".mp4"
                    self.assertFalse(
                        (publish / formal_name).exists(),
                        "a formal MP4 must not exist before quality and MP3 succeed",
                    )
                return QualityReport(
                    passed=True,
                    backend="fake-ffprobe",
                    elapsed_ms=1,
                    media={"video_codec": "h264", "width": 1080, "height": 1920},
                    checks=(),
                )

            job = RenderJob(
                batch_id="batch-1",
                platform_id="platform-1",
                source_file=str(source),
                title="The Locked Door",
                code="123456",
                video_folder=str(video_root),
                music_folder=str(music_root),
                output_folder=str(output_root),
                episode_number=2,
                episode_count=2,
                is_final_episode=True,
            )
            platform = PlatformProfile(
                id="platform-1",
                name="NovelBox",
                search_template="Search {platform}: {code}",
                ending_template=(
                    "Download {platform} and search code {code} to continue reading."
                ),
            )
            progress = []

            def planned_segments(_folder, target, **_kwargs):
                return [
                    VideoSegment(
                        path=video_path,
                        source_duration=max(60.0, target),
                        duration=target,
                    )
                ]

            def planned_music(_folder, mood, target, **_kwargs):
                return MusicPlan(music_path, max(target, 1.0), 1, mood)

            runner = PipelineRunner(
                lambda: settings,
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                text_provider_factory=text_factory,
                tts_provider_factory=tts_factory,
                command_runner=command_runner,
                quality_checker=quality_checker,
                usage_ledger=UsageLedger(root / "provider-usage.json"),
            )
            with (
                mock.patch(
                    "storyforge.pipeline.plan_video_segments",
                    side_effect=planned_segments,
                ),
                mock.patch(
                    "storyforge.pipeline.select_music_asset",
                    side_effect=planned_music,
                ),
                mock.patch(
                    "storyforge.pipeline.available_encoders",
                    return_value=["libx264"],
                ),
                mock.patch(
                    "storyforge.pipeline._commit_video_usage",
                    side_effect=PermissionError("read-only video ledger"),
                ) as commit_usage,
                mock.patch(
                    "storyforge.pipeline._commit_music_usage",
                    side_effect=PermissionError("read-only music ledger"),
                ) as commit_music,
            ):
                output = runner(
                    job,
                    platform,
                    lambda status, value, label: progress.append(
                        (status, value, label)
                    ),
                )
                preview_output = runner(
                    replace(
                        job,
                        job_kind="preview",
                        narration_audio_file="",
                    ),
                    platform,
                    lambda status, value, label: progress.append(
                        (status, value, label)
                    ),
                )

            output_path = Path(output)
            self.assertTrue(output_path.is_file())
            publish_dir = Path(job.publish_batch_folder)
            self.assertEqual(output_path.parent, publish_dir)
            self.assertEqual(
                {item.suffix for item in publish_dir.iterdir() if item.is_file()},
                {".mp4", ".mp3"},
            )
            job_dir = job_workspace_directory(job, root / "render-work")
            self.assertTrue((job_dir / "01-original.txt").is_file())
            self.assertTrue((job_dir / "02-narration-script.txt").is_file())
            self.assertFalse((job_dir / ".work" / "narration.wav").exists())
            narration_audio_path = Path(job.narration_audio_file)
            self.assertTrue(narration_audio_path.is_file())
            self.assertEqual(
                narration_audio_path.name,
                "001_NovelBox_123456_E002_V01_Bbatch-1.mp3",
            )
            self.assertEqual(narration_audio_path.parent, publish_dir)
            mp3_command = next(command for command in commands if "mp3" in command)
            self.assertIn("libmp3lame", mp3_command)
            self.assertEqual(mp3_command[mp3_command.index("-b:a") + 1], "192k")
            self.assertEqual(mp3_command[mp3_command.index("-ar") + 1], "48000")
            self.assertEqual(mp3_command[mp3_command.index("-ac") + 1], "2")
            self.assertTrue((job_dir / ".work" / "subtitles.ass").is_file())
            self.assertTrue((job_dir / "render-command.txt").is_file())
            self.assertTrue((job_dir / "manifest.json").is_file())
            self.assertTrue((job_dir / "quality-check.log").is_file())
            self.assertTrue(Path(preview_output).is_file())

            self.assertEqual(len(text_requests), 1)
            self.assertNotIn("Chapter 1", text_requests[0].text)
            self.assertNotIn("Chapter 2", text_requests[0].text)
            self.assertEqual(text_requests[0].text.count(CHAPTER_MARKER), 1)
            self.assertEqual(len(tts_calls), 2)
            spoken, selected_voice, selected_speed, stem = tts_calls[0]
            self.assertEqual(selected_voice, "af_bella")
            # Local Kokoro is calibrated at roughly 185 WPM for the short
            # narration chunks used by StoryForge.
            self.assertAlmostEqual(selected_speed, 240 / 185)
            self.assertEqual(stem, "line")
            self.assertFalse(any("Chapter" in sentence for sentence in spoken))
            self.assertFalse(any(CHAPTER_MARKER in sentence for sentence in spoken))
            self.assertIn("123456", spoken[-1])
            self.assertEqual(len(text_configs), 1)
            self.assertEqual(len(tts_configs), 2)

            expected_seconds = len(spoken) * 0.05 + 0.08
            subtitle_text = (job_dir / ".work" / "subtitles.ass").read_text(
                encoding="utf-8-sig"
            )
            self.assertIn("Search NovelBox: 123456", subtitle_text)
            self.assertIn("FINAL PART", subtitle_text)
            self.assertNotRegex(
                subtitle_text,
                r"(?m)^Dialogue: .*?,End(?:Title|Action|Code),",
            )
            self.assertRegex(
                subtitle_text.replace(r"\N", " "),
                r"(?m)^Dialogue: .*?,Subtitle,.*Download NovelBox and search code 123456",
            )
            self.assertNotIn("Part 2", subtitle_text)
            self.assertNotIn("Chapter 1", subtitle_text)
            self.assertNotIn(CHAPTER_MARKER, subtitle_text)
            preview_subtitle_text = (
                job_dir / ".work" / "preview-subtitles.ass"
            ).read_text(encoding="utf-8-sig")
            self.assertIn("FINAL PART", preview_subtitle_text)
            self.assertIn(
                "Download NovelBox and search code 123456",
                preview_subtitle_text.replace(r"\N", " "),
            )
            self.assertNotRegex(
                preview_subtitle_text,
                r"(?m)^Dialogue: .*?,End(?:Title|Action|Code),",
            )
            self.assertRegex(
                preview_subtitle_text.replace(r"\N", " "),
                r"(?m)^Dialogue: .*?,Subtitle,.*Download NovelBox and search code 123456",
            )
            self.assertNotIn("Part 2", preview_subtitle_text)

            preview_manifest = json.loads(
                (
                    job_dir / ".previews" / "preview-manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(preview_manifest["duration_seconds"], 15.0)
            self.assertEqual(
                preview_manifest["media"]["video_selection"]["mode"],
                "category_match",
            )
            self.assertFalse(
                preview_manifest["media"]["video_selection"]["fallback"]
            )
            self.assertTrue(preview_manifest["review_timeline"]["structured"])
            self.assertEqual(
                preview_manifest["review_timeline"]["opening_card"]["end_seconds"],
                4.0,
            )
            self.assertEqual(
                preview_manifest["review_timeline"]["story_body"],
                {
                    "start_seconds": 4.0,
                    "end_seconds": 10.0,
                    "narration_and_captions": True,
                },
            )
            self.assertEqual(
                preview_manifest["review_timeline"]["ending_card"]["start_seconds"],
                10.0,
            )
            self.assertEqual(
                preview_manifest["review_timeline"]["ending_card"]["kind"],
                "cover_caption",
            )
            self.assertIn("123456", preview_manifest["review_timeline"]["ending_card"]["narrated_cta"])
            self.assertTrue(Path(preview_output).name.endswith("_15s.mp4"))

            manifest = json.loads(
                (job_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["voice"]["voice"], "af_bella")
            self.assertEqual(manifest["voice"]["provider"], "local_kokoro")
            self.assertEqual(manifest["voice"]["requested_wpm"], 240)
            self.assertAlmostEqual(manifest["voice"]["speed_multiplier"], 240 / 185)
            self.assertAlmostEqual(
                manifest["voice"]["duration_seconds"],
                expected_seconds,
                places=6,
            )
            self.assertEqual(manifest["media"]["mood"], "romance")
            self.assertEqual(
                manifest["media"]["video_selection"]["mode"],
                "category_match",
            )
            self.assertEqual(
                manifest["media"]["video_selection"]["matched_category"],
                "romance",
            )
            self.assertFalse(manifest["media"]["video_selection"]["fallback"])
            self.assertEqual(manifest["media"]["encoder"], "libx264")
            self.assertEqual(manifest["media"]["ending_card"]["kind"], "cover_caption")
            self.assertTrue(manifest["media"]["ending_card"]["narrated_cta"])
            self.assertEqual(manifest["analysis"]["source_chapters"], 2)
            self.assertTrue(manifest["quality_control"]["passed"])
            self.assertTrue(manifest["media"]["narration_audio"]["enabled"])
            self.assertFalse(
                manifest["media"]["narration_audio"]["contains_background_music"]
            )
            self.assertEqual(
                manifest["media"]["narration_audio"]["output_file"],
                str(narration_audio_path),
            )
            self.assertEqual(
                manifest["result"]["narration_audio_file"],
                str(narration_audio_path),
            )
            self.assertEqual(
                manifest["job"]["narration_audio_file"],
                str(narration_audio_path),
            )
            self.assertTrue(
                any(
                    "素材使用次数记录失败（视频）" in warning
                    for warning in manifest["warnings"]
                )
            )
            self.assertTrue(
                any(
                    "素材使用次数记录失败（音乐）" in warning
                    for warning in manifest["warnings"]
                )
            )
            self.assertEqual(manifest["result"]["quality_log"], str(job_dir / "quality-check.log"))
            self.assertEqual(len(quality_expectations), 2)
            staged_video = quality_expectations[0][0]
            self.assertNotEqual(staged_video, output_path)
            self.assertEqual(staged_video.parent.parent.name, ".storyforge-staging")
            self.assertIn(output_root.resolve(), staged_video.parents)
            self.assertTrue(staged_video.name.endswith(".partial.mp4"))
            self.assertEqual(quality_expectations[0][1].width, 1080)
            self.assertEqual(quality_expectations[0][1].height, 1920)
            self.assertEqual(
                quality_expectations[0][1].checklist["promo_code_snapshot"],
                ("123456", "123456"),
            )
            self.assertEqual(quality_expectations[1][0], Path(preview_output))
            self.assertEqual(quality_expectations[1][1].width, 540)
            self.assertEqual(quality_expectations[1][1].height, 960)
            self.assertEqual(commands[0][-1], str(staged_video))
            mp3_stage = Path(mp3_command[-1])
            self.assertEqual(mp3_stage.parent, staged_video.parent)
            self.assertTrue(mp3_stage.name.endswith(".partial.mp3"))
            self.assertNotEqual(mp3_stage, narration_audio_path)
            self.assertIn("libx264", commands[0])
            commit_usage.assert_called_once()
            commit_music.assert_called_once()
            self.assertEqual(commit_music.call_args.args[0], str(music_root))
            self.assertEqual(commit_music.call_args.args[1].path, music_path)
            self.assertEqual(progress[0][0], JobStatus.PREFLIGHT)
            self.assertEqual(progress[-1][1], 0.98)

            failed_pair = replace(
                job,
                id="job-mp3-failure",
                variant_index=2,
                batch_ordinal=2,
                narration_audio_file="",
            )
            with (
                mock.patch(
                    "storyforge.pipeline.plan_video_segments",
                    side_effect=planned_segments,
                ),
                mock.patch(
                    "storyforge.pipeline.select_music_asset",
                    side_effect=planned_music,
                ),
                mock.patch(
                    "storyforge.pipeline.available_encoders",
                    return_value=["libx264"],
                ),
                mock.patch.object(
                    runner,
                    "_export_narration_mp3",
                    side_effect=PipelineError("synthetic MP3 failure"),
                ),
            ):
                with self.assertRaisesRegex(PipelineError, "完整成品已撤回"):
                    runner(failed_pair, platform, lambda *_args: None)
            failed_publish_dir = Path(failed_pair.publish_batch_folder)
            self.assertFalse(any("V02" in path.name for path in failed_publish_dir.iterdir()))
            self.assertEqual(
                list((output_root / ".storyforge-staging").rglob("*.partial.mp4")),
                [],
            )

    def test_render_failure_writes_log_and_does_not_commit_media_usage(self) -> None:
        """The late failure path must remain recoverable and not consume a clip use."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "123456_Story.txt"
            source.write_text("One complete sentence.", encoding="utf-8")
            for name in ("videos", "music", "output"):
                (root / name).mkdir()
            video_path = root / "videos" / "clip.mp4"
            music_path = root / "music" / "track.wav"
            video_path.write_bytes(b"placeholder")
            music_path.write_bytes(b"placeholder")
            settings = AppSettings()

            class TextProvider:
                def polish(self, request):
                    return TextResult(
                        polished_text=request.text,
                        hook=request.text,
                        ending_cta="Search code 123456 to continue.",
                        mood="suspense",
                        provider="fake",
                    )

            class TTSProvider:
                def synthesize(self, sentences, output_dir, *, voice, speed, file_stem):
                    segments = []
                    for index, sentence in enumerate(sentences, start=1):
                        path = Path(output_dir) / f"{file_stem}-{index}.wav"
                        _write_wav(path, 0.05)
                        segments.append(_speech_segment(index, sentence, path, 0.05))
                    return TTSResult(tuple(segments), provider="fake")

            def fail_render(command, **_kwargs):
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="synthetic ffmpeg failure"
                )

            runner = PipelineRunner(
                lambda: settings,
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                text_provider_factory=lambda _config: TextProvider(),
                tts_provider_factory=lambda _config: TTSProvider(),
                command_runner=fail_render,
            )
            job = RenderJob(
                batch_id="batch",
                platform_id="platform",
                source_file=str(source),
                title="Story",
                code="123456",
                video_folder=str(root / "videos"),
                music_folder=str(root / "music"),
                output_folder=str(root / "output"),
            )
            # Full productions reserve enough time for the required end card,
            # even when this synthetic narration is only a fraction of a second.
            segment_plan = [VideoSegment(video_path, 10.0, 10.0)]
            music_plan = MusicPlan(music_path, 10.0, 1, "suspense")
            with (
                mock.patch(
                    "storyforge.pipeline.plan_video_segments",
                    return_value=segment_plan,
                ),
                mock.patch(
                    "storyforge.pipeline.select_music_asset", return_value=music_plan
                ),
                mock.patch(
                    "storyforge.pipeline.available_encoders",
                    return_value=["libx264"],
                ),
                mock.patch("storyforge.pipeline._commit_video_usage") as commit_usage,
                mock.patch("storyforge.pipeline._commit_music_usage") as commit_music,
            ):
                with self.assertRaisesRegex(PipelineError, "FFmpeg") as raised:
                    runner(job, PlatformProfile(name="NovelBox"), lambda *_: None)
                commit_usage.assert_not_called()
                commit_music.assert_not_called()

            error_log = job_workspace_directory(job, runner.work_root) / "render-error.log"
            self.assertTrue(error_log.is_file())
            self.assertIn("synthetic ffmpeg failure", error_log.read_text(encoding="utf-8"))
            self.assertEqual(raised.exception.error_log, str(error_log))
            self.assertEqual(
                raised.exception.failure_diagnostics["code"],
                "unknown",
            )
            self.assertNotIn(str(root), raised.exception.failure_diagnostics["log_tail"])
            self.assertFalse(Path(job.publish_batch_folder).exists())
            self.assertFalse(any((root / "output").rglob("*.mp4")))
            self.assertEqual(
                list((root / "output" / ".storyforge-staging").rglob("*.partial.mp4")),
                [],
            )

    def test_quality_failure_marks_manifest_failed_before_committing_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "123456_Story.txt"
            source.write_text("One complete sentence.", encoding="utf-8")
            for name in ("videos", "music", "output"):
                (root / name).mkdir()
            video_path = root / "videos" / "clip.mp4"
            music_path = root / "music" / "track.wav"
            video_path.write_bytes(b"placeholder")
            music_path.write_bytes(b"placeholder")

            class TextProvider:
                def polish(self, request):
                    return TextResult(
                        polished_text=request.text,
                        hook=request.text,
                        ending_cta="Search code 123456 to continue.",
                        mood="suspense",
                        provider="fake",
                    )

            class TTSProvider:
                def synthesize(self, sentences, output_dir, *, voice, speed, file_stem):
                    segments = []
                    for index, sentence in enumerate(sentences, start=1):
                        path = Path(output_dir) / f"{file_stem}-{index}.wav"
                        _write_wav(path, 0.05)
                        segments.append(_speech_segment(index, sentence, path, 0.05))
                    return TTSResult(tuple(segments), provider="fake")

            def render(command, **_kwargs):
                Path(command[-1]).write_bytes(b"rendered-but-invalid")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            def reject_quality(_media_path, _expectation, **_kwargs):
                return QualityReport(
                    passed=False,
                    backend="fake-ffprobe",
                    elapsed_ms=1,
                    media={"video_codec": "hevc"},
                    checks=(),
                    errors=("video codec is not H.264",),
                )

            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                text_provider_factory=lambda _config: TextProvider(),
                tts_provider_factory=lambda _config: TTSProvider(),
                command_runner=render,
                quality_checker=reject_quality,
            )
            job = RenderJob(
                batch_id="batch",
                platform_id="platform",
                source_file=str(source),
                title="Story",
                code="123456",
                video_folder=str(root / "videos"),
                music_folder=str(root / "music"),
                output_folder=str(root / "output"),
            )
            segment_plan = [VideoSegment(video_path, 10.0, 10.0)]
            music_plan = MusicPlan(music_path, 10.0, 1, "suspense")
            with (
                mock.patch(
                    "storyforge.pipeline.plan_video_segments",
                    return_value=segment_plan,
                ),
                mock.patch(
                    "storyforge.pipeline.select_music_asset", return_value=music_plan
                ),
                mock.patch(
                    "storyforge.pipeline.available_encoders", return_value=["libx264"]
                ),
                mock.patch("storyforge.pipeline._commit_video_usage") as commit_usage,
                mock.patch("storyforge.pipeline._commit_music_usage") as commit_music,
            ):
                with self.assertRaisesRegex(PipelineError, "快速质检"):
                    runner(job, PlatformProfile(id="platform", name="NovelBox"), lambda *_: None)
                commit_usage.assert_not_called()
                commit_music.assert_not_called()

            job_dir = job_workspace_directory(job, runner.work_root)
            manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
            quality_log = job_dir / "quality-check.log"
            self.assertTrue(quality_log.is_file())
            self.assertIn('"passed": false', quality_log.read_text(encoding="utf-8"))
            self.assertFalse(manifest["quality_control"]["passed"])
            self.assertEqual(manifest["result"]["status"], "failed")
            self.assertEqual(manifest["result"]["error_log"], str(quality_log))
            self.assertEqual(manifest["result"]["output_file"], "")
            self.assertFalse(Path(job.publish_batch_folder).exists())
            self.assertFalse(any((root / "output").rglob("*.mp4")))

    def test_cancel_during_passing_quality_check_does_not_commit_media_usage(self) -> None:
        """A late cancellation must win before either durable usage ledger changes."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "123456_Story.txt"
            source.write_text("One complete sentence.", encoding="utf-8")
            for name in ("videos", "music", "output"):
                (root / name).mkdir()
            video_path = root / "videos" / "clip.mp4"
            music_path = root / "music" / "track.wav"
            video_path.write_bytes(b"placeholder")
            music_path.write_bytes(b"placeholder")

            class TextProvider:
                def polish(self, request):
                    return TextResult(
                        polished_text=request.text,
                        hook=request.text,
                        ending_cta="Search code 123456 to continue.",
                        mood="suspense",
                        provider="fake",
                    )

            class TTSProvider:
                def synthesize(self, sentences, output_dir, *, voice, speed, file_stem):
                    segments = []
                    for index, sentence in enumerate(sentences, start=1):
                        path = Path(output_dir) / f"{file_stem}-{index}.wav"
                        _write_wav(path, 0.05)
                        segments.append(_speech_segment(index, sentence, path, 0.05))
                    return TTSResult(tuple(segments), provider="fake")

            token = CancellationToken()

            def render(command, **_kwargs):
                Path(command[-1]).write_bytes(b"rendered-video")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            def cancel_then_pass(_media_path, _expectation, **_kwargs):
                token.cancel()
                return QualityReport(
                    passed=True,
                    backend="fake-ffprobe",
                    elapsed_ms=1,
                    media={"video_codec": "h264"},
                    checks=(),
                )

            runner = PipelineRunner(
                lambda: AppSettings(),
                ffmpeg_path=Path("fake-ffmpeg.exe"),
                text_provider_factory=lambda _config: TextProvider(),
                tts_provider_factory=lambda _config: TTSProvider(),
                command_runner=render,
                quality_checker=cancel_then_pass,
            )
            job = RenderJob(
                batch_id="batch",
                platform_id="platform",
                source_file=str(source),
                title="Story",
                code="123456",
                video_folder=str(root / "videos"),
                music_folder=str(root / "music"),
                output_folder=str(root / "output"),
            )
            segment_plan = [VideoSegment(video_path, 10.0, 10.0)]
            music_plan = MusicPlan(music_path, 10.0, 1, "suspense")
            with (
                mock.patch(
                    "storyforge.pipeline.plan_video_segments",
                    return_value=segment_plan,
                ),
                mock.patch(
                    "storyforge.pipeline.select_music_asset",
                    return_value=music_plan,
                ),
                mock.patch(
                    "storyforge.pipeline.available_encoders",
                    return_value=["libx264"],
                ),
                mock.patch("storyforge.pipeline._commit_video_usage") as commit_video,
                mock.patch("storyforge.pipeline._commit_music_usage") as commit_music,
                cancellation_scope(token),
            ):
                with self.assertRaises(JobCancelledError):
                    runner(
                        job,
                        PlatformProfile(id="platform", name="NovelBox"),
                        lambda *_: None,
                    )

            commit_video.assert_not_called()
            commit_music.assert_not_called()


if __name__ == "__main__":
    unittest.main()
